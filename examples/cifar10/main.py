from __future__ import print_function

import argparse
import os
import shutil
import time
import math

import numpy as np
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.nn.functional as F
import torch.optim
import torch.utils.data
import torchvision.transforms as transforms
import torchvision.datasets as datasets

import nics_fix_pt as nfp
import nics_fix_pt.nn_fix as nnf
from net import FixNet

parser = argparse.ArgumentParser(description="PyTorch Cifar10 Fixed-Point Training")
parser.add_argument(
    "--save-dir",
    required=True,
    help="The directory used to save the trained models",
    type=str,
)
parser.add_argument(
    "--gpu",
    metavar="GPUs",
    default="0",
    help="The gpu devices to use"
)
parser.add_argument(
    "--epoch",
    default=100,
    type=int,
    metavar="N",
    help="number of total epochs to run",
)
parser.add_argument(
    "--start-epoch",
    default=0,
    type=int,
    metavar="N",
    help="manual epoch number (useful on restarts)",
)
parser.add_argument(
    "-b",
    "--batch-size",
    default=128,
    type=int,
    metavar="N",
    help="mini-batch size (default: 128)",
)
parser.add_argument(
    "--test-batch-size",
    type=int,
    default=32,
    metavar="N",
    help="input batch size for testing (default: 1000)",
)
parser.add_argument(
    "--lr",
    "--learning-rate",
    default=0.05,
    type=float,
    metavar="LR",
    help="initial learning rate",
)
parser.add_argument("--momentum", default=0.9, type=float, metavar="M", help="momentum")
parser.add_argument(
    "--weight-decay",
    "--wd",
    default=5e-4,
    type=float,
    metavar="W",
    help="weight decay (default: 5e-4)",
)
parser.add_argument(
    "--print-freq",
    "-p",
    default=40,
    type=int,
    metavar="N",
    help="print frequency (default: 40)",
)
parser.add_argument(
    "--resume",
    default="",
    type=str,
    metavar="PATH",
    help="path to latest checkpoint (default: none)",
)
parser.add_argument(
    "--prefix",
    default="",
    type=str,
    metavar="PREFIX",
    help="checkpoint prefix (default: none)",
)
parser.add_argument(
    "--float-bn",
    default=False,
    action="store_true",
    help="quantize the bn layer"
)
parser.add_argument(
    "--fix-grad",
    default=False,
    action="store_true",
    help="quantize the gradients"
)
parser.add_argument(
    "--range-method",
    default=0,
    choices=[0, 1, 3],
    help=("range methods of data (including parameters, buffers, activations). "
          "0: RANGE_MAX, 1: RANGE_3SIGMA, 3: RANGE_SWEEP")
)
parser.add_argument(
    "--grad-range-method",
    default=0,
    choices=[0, 1, 3],
    help=("range methods of gradients (including parameters, activations)."
          " 0: RANGE_MAX, 1: RANGE_3SIGMA, 3: RANGE_SWEEP")
)
parser.add_argument(
    "-e",
    "--evaluate",
    action="store_true",
    help="evaluate model on validation set",
)
parser.add_argument(
    "--pretrained", default="", type=str, metavar="PATH", help="use pre-trained model"
)
parser.add_argument(
    "--bitwidth-data", default=8, type=int, help="the bitwidth of parameters/buffers/activations"
)
parser.add_argument(
    "--bitwidth-grad", default=16, type=int, help="the bitwidth of gradients of parameters/activations"
)

best_prec1 = 90
start = time.time()

def _set_fix_method_train_ori(model):
    model.set_fix_method(nfp.FIX_AUTO)

def _set_fix_method_eval_ori(model):
    model.set_fix_method(nfp.FIX_FIXED)

## --------
## When bitwidth is small, bn fix would prevent the model from learning.
## Could use this following config:
## Note that batchnorm2d_fix buffers (running_mean, running_var) are handled specially here.
## The running_mean and running_var are not quantized during training forward process,
## only quantized during test process. This could help avoid the buffer accumulation problem
## when the bitwidth is too small.
def _set_fix_method_train(model):
    model.set_fix_method(
        nfp.FIX_AUTO,
        method_by_type={
            "BatchNorm2d_fix": {
                "weight": nfp.FIX_AUTO,
                "bias": nfp.FIX_AUTO,
                "running_mean": nfp.FIX_NONE,
                "running_var": nfp.FIX_NONE}
        })

def _set_fix_method_eval(model):
    model.set_fix_method(
        nfp.FIX_FIXED,
        method_by_type={
            "BatchNorm2d_fix": {
                "weight": nfp.FIX_FIXED,
                "bias": nfp.FIX_FIXED,
                "running_mean": nfp.FIX_AUTO,
                "running_var": nfp.FIX_AUTO}
        })
## --------


def main():
    global args, best_prec1
    args = parser.parse_args()
    print("cmd line arguments: ", args)

    gpus = [int(d) for d in args.gpu.split(",")]
    torch.cuda.set_device(gpus[0])
        
    # Check the save_dir exists or not
    if not os.path.exists(args.save_dir):
        os.makedirs(args.save_dir)

    model = FixNet(
        fix_bn=not args.float_bn,
        fix_grad=args.fix_grad,
        range_method=args.range_method,
        grad_range_method=args.grad_range_method,
        bitwidth_data=args.bitwidth_data,
        bitwidth_grad=args.bitwidth_grad
    )
    model.print_fix_configs()

    model.cuda()
    if len(gpus) > 1:
        parallel_model = torch.nn.DataParallel(model, gpus)
    else:
        parallel_model = model

    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint["epoch"]
            best_prec1 = checkpoint["best_prec1"]
            model.load_state_dict(checkpoint["state_dict"])
            print(
                "=> loaded checkpoint '{}' (epoch {})".format(
                    args.evaluate, checkpoint["epoch"]
                )
            )
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))
            assert os.path.isfile(args.resume)

    if args.pretrained:
        if os.path.isfile(args.pretrained):
            print("=> fintune from checkpoint '{}'".format(args.pretrained))
            checkpoint = torch.load(args.pretrained)
            # args.start_epoch = checkpoint['epoch']
            # best_prec1 = checkpoint['best_prec1']
            model.load_state_dict(checkpoint["state_dict"])
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))
            assert os.path.isfile(args.pretrained)

    # cudnn.benchmark = True

    normalize = transforms.Normalize(
        mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
    )

    train_loader = torch.utils.data.DataLoader(
        datasets.CIFAR10(
            root="../data/cifar10",
            train=True,
            transform=transforms.Compose(
                [
                    transforms.RandomHorizontalFlip(),
                    transforms.RandomCrop(32, 4),
                    transforms.ToTensor(),
                    normalize,
                ]
            ),
            download=True,
        ),
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=4,
        pin_memory=True,
    )

    val_loader = torch.utils.data.DataLoader(
        datasets.CIFAR10(
            root="../data/cifar10",
            train=False,
            transform=transforms.Compose([transforms.ToTensor(), normalize]),
        ),
        batch_size=args.test_batch_size,
        shuffle=False,
        num_workers=2,
        pin_memory=True,
    )

    # define loss function (criterion) and pptimizer
    criterion = nn.CrossEntropyLoss().cuda()

    optimizer = torch.optim.SGD(
        model.parameters(),
        lr=args.lr,
        momentum=args.momentum,
        weight_decay=args.weight_decay,
    )

    if args.evaluate:
        validate(val_loader, model, parallel_model, criterion)
        return

    for epoch in range(args.start_epoch, args.epoch):
        adjust_learning_rate(optimizer, epoch)

        # train for one epoch
        train(train_loader, model, parallel_model, criterion, optimizer, epoch)

        # evaluate on validation set
        prec1 = validate(val_loader, model, parallel_model, criterion)

        # remember best prec@1 and save checkpoint
        is_best = prec1 > best_prec1
        best_prec1 = max(prec1, best_prec1)
        if best_prec1 > 90 and is_best:
            save_checkpoint(
                {
                    "epoch": epoch + 1,
                    "state_dict": model.state_dict(),
                    "best_prec1": best_prec1,
                },
                is_best,
                filename=os.path.join(
                    args.save_dir,
                    "checkpoint_{}_{:.3f}.tar".format(args.prefix, best_prec1),
                ),
            )
            model.print_fix_configs()

    print("Best acc: {}".format(best_prec1))


def train(train_loader, model, p_model, criterion, optimizer, epoch):
    """
        Run one train epoch
    """
    losses = AverageMeter()
    top1 = AverageMeter()

    # switch to train mode
    _set_fix_method_train_ori(model)
    model.train()

    for i, (input, target) in enumerate(train_loader):
        target = target.cuda(async=True)
        input_var = torch.autograd.Variable(input).cuda()
        target_var = torch.autograd.Variable(target)

        # compute output
        output = p_model(input_var)
        loss = criterion(output, target_var)

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        output = output.float()
        loss = loss.float()
        # measure accuracy and record loss
        prec1 = accuracy(output.data, target)[0]
        losses.update(loss.item(), input.size(0))
        top1.update(prec1.item(), input.size(0))

        if i % args.print_freq == 0:
            print(
                "\rEpoch: [{0}][{1}/{2}]\t"
                "Time {t}\t"
                "Loss {loss.val:.4f} ({loss.avg:.4f})\t"
                "Prec@1 {top1.val:.3f}% ({top1.avg:.3f}%)".format(
                    epoch,
                    i,
                    len(train_loader),
                    t=time.time() - start,
                    loss=losses,
                    top1=top1,
                ),
                end="",
            )


def validate(val_loader, model, p_model, criterion):
    """
    Run evaluation
    """
    losses = AverageMeter()
    top1 = AverageMeter()

    # switch to evaluate mode
    _set_fix_method_eval_ori(model)
    model.eval()

    with torch.no_grad():
        for i, (input, target) in enumerate(val_loader):
            target = target.cuda(async=True)
            input_var = torch.autograd.Variable(input).cuda()
            target_var = torch.autograd.Variable(target)
    
            # compute output
            output = p_model(input_var)
            loss = criterion(output, target_var)
    
            output = output.float()
            loss = loss.float()
    
            # measure accuracy and record loss
            prec1 = accuracy(output.data, target)[0]
            losses.update(loss.item(), input.size(0))
            top1.update(prec1.item(), input.size(0))
    
            if i % args.print_freq == 0:
                print(
                    "Test: [{0}/{1}]\t"
                    "Time {t}\t"
                    "Loss {loss.val:.4f} ({loss.avg:.4f})\t"
                    "Prec@1 {top1.val:.3f}% ({top1.avg:.3f}%)".format(
                        i, len(val_loader), t=time.time() - start, loss=losses, top1=top1
                    )
                )

    print(
        " * Prec@1 {top1.avg:.3f}%\tBest Prec@1 {best_prec1:.3f}%".format(
            top1=top1, best_prec1=best_prec1
        )
    )

    return top1.avg


def save_checkpoint(state, is_best, filename="checkpoint.pth.tar"):
    """
    Save the training model
    """
    torch.save(state, filename)


class AverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def adjust_learning_rate(optimizer, epoch):
    """Sets the learning rate to the initial LR decayed by 0.5 every 10 epochs"""
    lr = args.lr * (0.5 ** (epoch // 10))
    for param_group in optimizer.param_groups:
        param_group["lr"] = lr
    print("Epoch {}: lr: {}".format(epoch, lr))

def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res


if __name__ == "__main__":
    main()
