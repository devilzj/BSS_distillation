'''Train CIFAR10 with PyTorch.'''
from __future__ import print_function

import argparse
import os
import time

import numpy as np
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torchvision
import torchvision.transforms as transforms
import torch.nn.functional as F
import attacks

from models import *

dataset_name = 'CIFAR-10'
res_folder = 'results/BSS_distillation_80epoch_res8_C10'
temperature = 3
gpu_num = 0
attack_size = 64
max_epoch = 80

if not os.path.isdir(res_folder):
    os.mkdir(res_folder)

use_cuda = torch.cuda.is_available()

# Data
if dataset_name is 'CIFAR-10':
    # CIFAR-10
    print('==> Preparing data..')
    transform_train = transforms.Compose([
        transforms.RandomCrop(32, padding=4),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
    ])

    transform_test = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
    ])

    trainset = torchvision.datasets.CIFAR10(root='./data', train=True, download=True, transform=transform_train)
    trainloader = torch.utils.data.DataLoader(trainset, batch_size=256, shuffle=True, num_workers=4)

    testset = torchvision.datasets.CIFAR10(root='./data', train=False, download=True, transform=transform_test)
    testloader = torch.utils.data.DataLoader(testset, batch_size=100, shuffle=False, num_workers=4)
else:
    raise Exception('Undefined Dataset')

# Model
if dataset_name is 'CIFAR-10':
    t_net = torch.load('./results/Res26_zero_init_C10/320_epoch.t7', map_location=lambda storage, loc: storage.cuda(0))['net']
    BN_version_fix(t_net)
    state_t = t_net.state_dict()
    teacher = ResNet26_zero()
    teacher.load_state_dict(state_t)

    student = ResNet8_zero()

t_net = Distill_ResNet_Simple(teacher)
s_net = Distill_ResNet_Simple(student)

if use_cuda:
    torch.cuda.set_device(gpu_num)
    t_net.cuda()
    s_net.cuda()
    cudnn.benchmark = True

attack = attacks.AttackBSS(targeted=True, num_steps=10, max_epsilon=16, step_alpha=0.3, cuda=True, norm=2)

criterion_MSE = nn.MSELoss(size_average=False)
criterion_CE = nn.CrossEntropyLoss()

# Training
def train_attack_CE(t_net, s_net, ratio, ratio_attack, epoch):
    epoch_start_time = time.time()
    print('\nStage 1 Epoch: %d' % epoch)
    s_net.train()
    t_net.eval()
    train_loss = 0
    correct = 0
    total = 0
    global optimizer
    for batch_idx, (inputs, targets) in enumerate(trainloader):
        if use_cuda:
            inputs, targets = inputs.cuda(), targets.cuda()

        batch_size1 = inputs.shape[0]

        optimizer.zero_grad()
        inputs, targets = Variable(inputs), Variable(targets)

        outputs = s_net(inputs)

        # Cross-entropy loss
        loss = criterion_CE(outputs[0:batch_size1, :], targets)
        t_net(inputs)

        # KD loss
        loss += - ratio * (F.softmax(t_net.out/temperature, 1).detach() * F.log_softmax(s_net.out/temperature, 1)).sum() / batch_size1

        if ratio_attack > 0:

            attack_flag = targets.data == t_net.out.sort(dim=1, descending=True)[1][:, 0].data

            if attack_flag.sum():
                # Base sample selection
                attack_idx = attack_flag.nonzero().squeeze()
                if attack_idx.shape[0] > attack_size:
                    diff = (F.softmax(t_net.out[attack_idx,:], 1).data - F.softmax(s_net.out[attack_idx,:], 1).data) ** 2
                    distill_score = diff.sum(dim=1) - diff.gather(1, targets[attack_idx].data.unsqueeze(1)).squeeze()
                    attack_idx = attack_idx[distill_score.sort(descending=True)[1][:attack_size]]

                # Target class sampling
                attack_class = t_net.out.sort(dim=1, descending=True)[1][:, 1][attack_idx].data
                class_score, class_idx = F.softmax(t_net.out, 1)[attack_idx, :].data.sort(dim=1, descending=True)
                class_score = class_score[:, 1:]
                class_idx = class_idx[:, 1:]

                rand_seed = 1 * (class_score.sum(dim=1) * torch.rand([attack_idx.shape[0]]).cuda()).unsqueeze(1)
                prob = class_score.cumsum(dim=1)
                for k in range(attack_idx.shape[0]):
                    for c in range(prob.shape[1]):
                        if (prob[k, c] >= rand_seed[k]).cpu().numpy():
                            attack_class[k] = class_idx[k, c]
                            break

                # Forward and backward for adversarial samples
                attacked_inputs = Variable(attack.run(t_net, inputs[attack_idx, :, :, :].data, attack_class))
                batch_size2 = attacked_inputs.shape[0]

                t_net(attacked_inputs)
                s_net(attacked_inputs)

                # KD loss for Boundary Supporting Samples (BSS)
                loss += - ratio_attack * (F.softmax(t_net.out / temperature, 1).detach() * F.log_softmax(s_net.out / temperature, 1)).sum() / batch_size2

        loss.backward()
        optimizer.step()

        train_loss += loss.data.item()
        _, predicted = torch.max(outputs[0:batch_size1, :].data, 1)
        total += targets.size(0)
        correct += predicted.eq(targets.data).cpu().float().sum()
        b_idx = batch_idx

    print('Train \t Time Taken: %.2f sec' % (time.time() - epoch_start_time))
    print('Loss: %.3f | Acc: %.3f%% (%d/%d)' % (train_loss / (b_idx + 1), 100. * correct / total, correct, total))

def test(net, epoch, save=False):
    epoch_start_time = time.time()
    net.eval()
    test_loss = 0
    correct = 0
    total = 0
    for batch_idx, (inputs, targets) in enumerate(testloader):
        if use_cuda:
            inputs, targets = inputs.cuda(), targets.cuda()
        inputs, targets = Variable(inputs), Variable(targets)
        outputs = net(inputs)
        loss = criterion_CE(outputs, targets)

        test_loss += loss.data.item()
        _, predicted = torch.max(outputs.data, 1)
        total += targets.size(0)
        correct += predicted.eq(targets.data).cpu().float().sum()
        b_idx= batch_idx

    print('Test \t Time Taken: %.2f sec' % (time.time() - epoch_start_time))
    print('Loss: %.3f | Acc: %.3f%% (%d/%d)' % (test_loss/(b_idx+1), 100.*correct/total, correct, total))

    if save:
        # Save checkpoint.
        acc = 100.*correct/total
        if epoch is not 0 and epoch % 80 is 0:
            print('Saving..')
            state = {
                'net': net if use_cuda else net,
                'acc': acc,
                'epoch': epoch,
            }
            torch.save(state, './' + res_folder + '/%d_epoch.t7' % epoch)

for epoch in range(1, max_epoch+1):
    if epoch == 1:
        optimizer = optim.SGD(s_net.parameters(), lr=0.1, momentum=0.9, weight_decay=1e-4)
    elif epoch == max_epoch/2:
        optimizer = optim.SGD(s_net.parameters(), lr=0.01, momentum=0.9, weight_decay=1e-4)
    elif epoch == max_epoch/4*3:
        optimizer = optim.SGD(s_net.parameters(), lr=0.001, momentum=0.9, weight_decay=1e-4)

    ratio = max(3 * (1 - epoch / max_epoch), 0) + 1
    attack_ratio = max(2 * (1 - 4 / 3 * epoch / max_epoch), 0) + 0

    train_attack_CE(t_net, s_net, ratio, attack_ratio, epoch)

    test(s_net, epoch, save=True)

state = {
    'net': s_net,
    'epoch': max_epoch,
}
torch.save(state, './' + res_folder + '/%depoch_final.t7' % (max_epoch))