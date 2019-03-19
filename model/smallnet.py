import torch
import torch.nn as nn
import torch.nn.functional as F
from base import BaseModel


# NeurIPS 2017 style network

class SmallNet(BaseModel):
    def __init__(self, num_output_channels=16):
        super(SmallNet, self).__init__()

        self.conv1 = self._generate_conv_block(3, 20, kernel_size=5, padding=2)
        self.mp = nn.MaxPool2d(kernel_size=2, stride=2)
        self.conv2 = self._generate_conv_block(20, 48, kernel_size=5, padding=2)
        self.conv3 = self._generate_conv_block(48, 64, kernel_size=5, padding=4, dilation=2)
        self.conv4 = self._generate_conv_block(64, 80, kernel_size=3, padding=4, dilation=4)
        self.conv5 = self._generate_conv_block(80, 256, kernel_size=3, padding=2, dilation=2)
        self.conv6 = self._generate_conv_block(256, num_output_channels, kernel_size=1, padding=0)

    def _generate_conv_block(self, in_channels, out_channels, kernel_size, stride=1, padding=0, dilation=1):
        conv = nn.Conv2d(in_channels, out_channels, kernel_size, stride, padding, dilation)
        bn = nn.BatchNorm2d(out_channels)
        relu = nn.LeakyReLU(0.2)
        return nn.Sequential(conv, bn, relu)

    def forward(self, x):
        x = self.conv1(x)
        x = self.mp(x)
        x = self.conv2(x)
        x = self.conv3(x)
        x = self.conv4(x)
        x = self.conv5(x)
        x = self.conv6(x)

        return [x]

if __name__ == '__main__':
    x = torch.randn(2,3,70,70)
    net = SmallNet(num_output_channels=4)
    y = net.forward(x)
    print(y[0].shape)