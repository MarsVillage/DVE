from torchvision import datasets, transforms
import torchvision.transforms.functional as TF
from torch.utils.data import DataLoader
from torch.utils.data.dataset import Dataset
import torch
from utils import tps

import numpy as np
import pandas as pd
import os
from PIL import Image

from io import BytesIO


class PcaAug(object):
    _eigval = torch.Tensor([0.2175, 0.0188, 0.0045])
    _eigvec = torch.Tensor([
        [-0.5675, 0.7192, 0.4009],
        [-0.5808, -0.0045, -0.8140],
        [-0.5836, -0.6948, 0.4203],
    ])

    def __init__(self, alpha=0.1):
        self.alpha = alpha

    def __call__(self, im):
        alpha = torch.randn(3) * self.alpha
        rgb = (self._eigvec * alpha.expand(3, 3) * self._eigval.expand(3, 3)).sum(1)
        return im + rgb.reshape(3, 1, 1)


class JPEGNoise(object):
    def __init__(self, low=30, high=99):
        self.low = low
        self.high = high

    def __call__(self, im):
        H = im.height
        W = im.width
        rW = max(int(0.8 * W), int(W * (1 + 0.5 * torch.randn([]))))
        im = TF.resize(im, (rW, rW))
        buf = BytesIO()
        im.save(buf, format='JPEG', quality=torch.randint(self.low, self.high, []).item())
        im = Image.open(buf)
        im = TF.resize(im, (H, W))
        return im


def kp_normalize(H, W, kp):
    kp = kp.clone()
    kp[..., 0] = 2. * kp[..., 0] / (W - 1) - 1
    kp[..., 1] = 2. * kp[..., 1] / (H - 1) - 1
    return kp


class CelebABase(Dataset):
    def __getitem__(self, index):
        im = Image.open(os.path.join(self.root, 'Img', 'img_align_celeba', self.filenames[index]))
        kp = None
        if self.use_keypoints:
            kp = self.keypoints[index].copy()
        meta = {}

        if self.warper is not None:
            if self.warper.returns_pairs:
                im1 = self.initial_transforms(im)
                im1 = TF.to_tensor(im1) * 255

                im1, im2, flow, grid, kp1, kp2 = self.warper(im1, keypts=kp, crop=self.crop)

                im1 = im1.to(torch.uint8)
                im2 = im2.to(torch.uint8)

                C, H, W = im1.shape

                im1 = TF.to_pil_image(im1)
                im2 = TF.to_pil_image(im2)

                im1 = self.transforms(im1)
                im2 = self.transforms(im2)

                C, H, W = im1.shape
                data = torch.stack((im1, im2), 0)
                meta = {'flow': flow[0], 'grid': grid[0], 'im1': im1, 'im2': im2, 'index': index}
                if self.use_keypoints:
                    meta = {**meta, **{'kp1': kp1, 'kp2': kp2}}
            else:
                im1 = self.initial_transforms(im)
                im1 = TF.to_tensor(im1) * 255

                im1, kp = self.warper(im1, keypts=kp, crop=self.crop)

                im1 = im1.to(torch.uint8)
                im1 = TF.to_pil_image(im1)
                im1 = self.transforms(im1)

                C, H, W = im1.shape
                data = im1
                if self.use_keypoints:
                    meta = {'keypts': kp, 'keypts_normalized': kp_normalize(H, W, kp), 'index': index}

        else:
            data = self.transforms(self.initial_transforms(im))

            if self.crop != 0:
                data = data[:, self.crop:-self.crop, self.crop:-self.crop]
                kp = kp - self.crop
                kp = torch.tensor(kp)

            C, H, W = data.shape
            if self.use_keypoints:
                meta = {'keypts': kp, 'keypts_normalized': kp_normalize(H, W, kp), 'index': index}

        return data, meta


class CelebAPrunedAligned_MAFLVal(CelebABase):
    eye_kp_idxs = [0, 1]

    def __init__(self, root, train=True, pair_warper=None, imwidth=100, crop=18, do_augmentations=True,
                 use_keypoints=False):
        self.root = root
        self.imwidth = imwidth
        self.train = train
        self.warper = pair_warper
        self.crop = crop
        self.use_keypoints = use_keypoints

        anno = pd.read_csv(os.path.join(root, 'Anno', 'list_landmarks_align_celeba.txt'), header=1,
                           delim_whitespace=True)
        assert len(anno.index) == 202599
        split = pd.read_csv(os.path.join(root, 'Eval', 'list_eval_partition.txt'), header=None,
                            delim_whitespace=True, index_col=0)
        assert len(split.index) == 202599

        mafltest = pd.read_csv(os.path.join(root, 'MAFL', 'testing.txt'), header=None, delim_whitespace=True,
                               index_col=0)
        split.loc[mafltest.index] = 4
        assert (split[1] == 4).sum() == 1000

        if train:
            self.data = anno.loc[split[split[1] == 0].index]
        else:
            self.data = anno.loc[split[split[1] == 4].index]

        # lefteye_x lefteye_y ; righteye_x righteye_y ; nose_x nose_y ; leftmouth_x leftmouth_y ; rightmouth_x rightmouth_y
        self.keypoints = np.array(self.data, dtype=np.float32).reshape(-1, 5, 2)

        self.filenames = list(self.data.index)

        # Move head up a bit
        initial_crop = lambda im: transforms.functional.crop(im, 30, 0, 178, 178)
        self.keypoints[:, :, 1] -= 30
        self.keypoints *= self.imwidth / 178.

        normalize = transforms.Normalize(mean=[0.5084, 0.4224, 0.3769], std=[0.2599, 0.2371, 0.2323])
        augmentations = [JPEGNoise(), transforms.transforms.ColorJitter(.4, .4, .4),
                         transforms.ToTensor(), PcaAug()] if (train and do_augmentations) else [transforms.ToTensor()]

        self.initial_transforms = transforms.Compose([initial_crop, transforms.Resize(self.imwidth)])
        self.transforms = transforms.Compose(augmentations + [normalize])

    def __len__(self):
        return len(self.data.index)


class MAFLAligned(CelebABase):
    eye_kp_idxs = [0, 1]

    def __init__(self, root, train=True, pair_warper=None, imwidth=100, crop=18, do_augmentations=True,
                 use_keypoints=False):
        self.root = root
        self.imwidth = imwidth
        self.train = train
        self.warper = pair_warper
        self.crop = crop
        self.use_keypoints = use_keypoints

        anno = pd.read_csv(os.path.join(root, 'Anno', 'list_landmarks_align_celeba.txt'), header=1,
                           delim_whitespace=True)
        assert len(anno.index) == 202599
        split = pd.read_csv(os.path.join(root, 'Eval', 'list_eval_partition.txt'), header=None,
                            delim_whitespace=True, index_col=0)
        assert len(split.index) == 202599

        mafltest = pd.read_csv(os.path.join(root, 'MAFL', 'testing.txt'), header=None, delim_whitespace=True,
                               index_col=0)
        split.loc[mafltest.index] = 4

        mafltrain = pd.read_csv(os.path.join(root, 'MAFL', 'training.txt'), header=None, delim_whitespace=True,
                                index_col=0)
        split.loc[mafltrain.index] = 5

        assert (split[1] == 4).sum() == 1000
        assert (split[1] == 5).sum() == 19000

        if train:
            self.data = anno.loc[split[split[1] == 5].index]
        else:
            self.data = anno.loc[split[split[1] == 4].index]

        # lefteye_x lefteye_y ; righteye_x righteye_y ; nose_x nose_y ; leftmouth_x leftmouth_y ; rightmouth_x rightmouth_y
        self.keypoints = np.array(self.data, dtype=np.float32).reshape(-1, 5, 2)

        self.filenames = list(self.data.index)

        # Move head up a bit
        initial_crop = lambda im: transforms.functional.crop(im, 30, 0, 178, 178)
        self.keypoints[:, :, 1] -= 30
        self.keypoints *= self.imwidth / 178.

        normalize = transforms.Normalize(mean=[0.5084, 0.4224, 0.3769], std=[0.2599, 0.2371, 0.2323])
        augmentations = [JPEGNoise(), transforms.transforms.ColorJitter(.4, .4, .4),
                         transforms.ToTensor(), PcaAug()] if (train and do_augmentations) else [transforms.ToTensor()]

        self.initial_transforms = transforms.Compose([initial_crop, transforms.Resize(self.imwidth)])
        self.transforms = transforms.Compose(augmentations + [normalize])

    def __len__(self):
        return len(self.data.index)


class AFLW_MTFL(Dataset):
    """Used for testing on the 5-point version of AFLW included in the MTFL download from the
       Facial Landmark Detection by Deep Multi-task Learning (TCDCN) paper
       http://mmlab.ie.cuhk.edu.hk/projects/TCDCN.html

       For training this can use either MAFL or a cropped 5-point version of AFLW used in
       http://openaccess.thecvf.com/content_ICCV_2017/papers/Thewlis_Unsupervised_Learning_of_ICCV_2017_paper.pdf
       """
    eye_kp_idxs = [0, 1]

    def __init__(self, train_root, test_root, train_set='aflw_cropped', train=True, pair_warper=None, imwidth=70,
                 crop=0, do_augmentations=True, use_keypoints=False):
        self.test_root = test_root  # MTFL from http://mmlab.ie.cuhk.edu.hk/projects/TCDCN/data/MTFL.zip
        self.train_root = train_root  # AFLW cropped from http://www.robots.ox.ac.uk/~jdt/aflw_cropped.zip
        self.train_set = train_set  # 'aflw_cropped' or 'mafl'

        self.imwidth = imwidth
        self.train = train
        self.warper = pair_warper
        self.crop = crop
        self.use_keypoints = use_keypoints

        initial_crop = lambda im: im

        test_anno = pd.read_csv(os.path.join(test_root, 'testing.txt'), header=None, delim_whitespace=True)

        if train:
            self.root = train_root
            if train_set == 'aflw_cropped':
                all_anno = pd.read_csv(os.path.join(train_root, 'facedata_cropped.csv'), sep=',', header=0)
                allims = all_anno.image_file.to_list()
                trainims = all_anno[all_anno.set == 1].image_file.to_list()
                testims = [t.split('-')[-1] for t in test_anno.loc[:, 0].to_list()]

                for x in trainims:
                    assert x not in testims

                for x in testims:
                    assert x in allims

                self.filenames = all_anno[all_anno.set == 1].crop_file.to_list()
                self.keypoints = np.array(all_anno[all_anno.set == 1].iloc[:, 4:14], dtype=np.float32).reshape(-1, 5, 2)

                self.keypoints -= 1  # matlab to python
                self.keypoints *= self.imwidth / 150.

                assert len(self.filenames) == 10122

            if train_set == 'mafl':
                # todo will probably need different imwidth/crop for train vs test
                # should maybe just allow setting different datasets for train/test rather than making it an option here
                anno = pd.read_csv(os.path.join(train_root, 'Anno', 'list_landmarks_align_celeba.txt'), header=1,
                                   delim_whitespace=True)
                split = pd.read_csv(os.path.join(train_root, 'Eval', 'list_eval_partition.txt'), header=None,
                                    delim_whitespace=True, index_col=0)
                mafltrain = pd.read_csv(os.path.join(train_root, 'MAFL', 'training.txt'), header=None,
                                        delim_whitespace=True,
                                        index_col=0)
                split.loc[mafltrain.index] = 5
                data = anno.loc[split[split[1] == 5].index]

                self.keypoints = np.array(data, dtype=np.float32).reshape(-1, 5, 2)
                self.filenames = list(data.index)

                # Move head up a bit
                initial_crop = lambda im: transforms.functional.crop(im, 30, 0, 178, 178)
                self.keypoints[:, :, 1] -= 30
                self.keypoints *= self.imwidth / 178.

        else:
            self.root = test_root
            self.keypoints = np.array(test_anno.iloc[:, 1:11], dtype=np.float32).reshape(-1, 2, 5).transpose(0,2,1)
            self.filenames = test_anno[0].to_list()

            self.keypoints -= 1  # matlab to python
            self.keypoints *= self.imwidth / 150.

            assert len(self.filenames) == 2995



        normalize = transforms.Normalize(mean=[0.5084, 0.4224, 0.3769], std=[0.2599, 0.2371, 0.2323])
        augmentations = [JPEGNoise(), transforms.transforms.ColorJitter(.4, .4, .4),
                         transforms.ToTensor(), PcaAug()] if (train and do_augmentations) else [transforms.ToTensor()]

        self.initial_transforms = transforms.Compose([initial_crop, transforms.Resize(self.imwidth)])
        self.transforms = transforms.Compose(augmentations + [normalize])

    def __len__(self):
        return len(self.filenames)

    def __getitem__(self, index):
        im = Image.open(os.path.join(self.root, self.filenames[index]))
        kp = None
        if self.use_keypoints:
            kp = self.keypoints[index].copy()
        meta = {}

        if self.warper is not None:
            if self.warper.returns_pairs:
                im1 = self.initial_transforms(im)
                im1 = TF.to_tensor(im1) * 255

                im1, im2, flow, grid, kp1, kp2 = self.warper(im1, keypts=kp, crop=self.crop)

                im1 = im1.to(torch.uint8)
                im2 = im2.to(torch.uint8)

                C, H, W = im1.shape

                im1 = TF.to_pil_image(im1)
                im2 = TF.to_pil_image(im2)

                im1 = self.transforms(im1)
                im2 = self.transforms(im2)

                C, H, W = im1.shape
                data = torch.stack((im1, im2), 0)
                meta = {'flow': flow[0], 'grid': grid[0], 'im1': im1, 'im2': im2, 'index': index}
                if self.use_keypoints:
                    meta = {**meta, **{'kp1': kp1, 'kp2': kp2}}
            else:
                im1 = self.initial_transforms(im)
                im1 = TF.to_tensor(im1) * 255

                im1, kp = self.warper(im1, keypts=kp, crop=self.crop)

                im1 = im1.to(torch.uint8)
                im1 = TF.to_pil_image(im1)
                im1 = self.transforms(im1)

                C, H, W = im1.shape
                data = im1
                if self.use_keypoints:
                    meta = {'keypts': kp, 'keypts_normalized': kp_normalize(H, W, kp), 'index': index}

        else:
            data = self.transforms(self.initial_transforms(im))

            if self.crop != 0:
                data = data[:, self.crop:-self.crop, self.crop:-self.crop]
                kp = kp - self.crop
                kp = torch.tensor(kp)

            C, H, W = data.shape
            if self.use_keypoints:
                meta = {'keypts': kp, 'keypts_normalized': kp_normalize(H, W, kp), 'index': index}

        return data, meta


if __name__ == '__main__':
    import matplotlib
    matplotlib.use('TkAgg')
    import pylab

    #dataset = CelebAPrunedAligned_MAFLVal('data/celeba', True, pair_warper=tps.Warper(100, 100), use_keypoints=True)
    dataset = AFLW_MTFL('data/aflw_cropped', 'data/MTFL', 'aflw_cropped', True, pair_warper=tps.Warper(70, 70), use_keypoints=True, imwidth=70)

    x, meta = dataset[6]
    print(x[0].shape)
    pylab.imshow(x[0].permute(1, 2, 0) + 0.5)
    kp1 = meta['kp1']
    pylab.scatter(kp1[:,0], kp1[:,1], c=np.arange(5))
    pylab.figure()
    pylab.imshow(x[1].permute(1, 2, 0) + 0.5)
    kp2 = meta['kp2']
    pylab.scatter(kp2[:,0], kp2[:,1], c=np.arange(5))
    pylab.show()
