"""
ipy data_loader/data_loaders.py -- \
        --dataset CelebAPrunedAligned_MAFLVal \
        --root ~/data/shared-datasets/celeba \
        --use_keypoints

ipy data_loader/data_loaders.py -- \
        --dataset AFLW \
        --root ~/data/shared-datasets/aflw

ipy data_loader/data_loaders.py -- \
        --dataset Chimps
"""
import numpy as np
import pandas as pd
import os
from PIL import Image
from utils import tps
import glob
import torch
from os.path import join as pjoin
from utils.util import label_colormap
from utils.util import pad_and_crop
from scipy.io import loadmat
from torchvision import transforms
import torchvision.transforms.functional as TF
from torch.utils.data.dataset import Dataset
from data_loader.augmentations import get_composed_augmentations

from io import BytesIO
import sys
from pathlib import Path
import matplotlib

if sys.platform == 'darwin':
    matplotlib.use("macosx")
import matplotlib.pyplot as plt  # NOQA

sys.path.insert(0, str(Path.home() / "coding/src/zsvision/python"))
try:
    from zsvision.zs_iterm import zs_dispFig  # NOQA
except:
    zs_dispFig = lambda: None
    print('No zs_dispFig, figures will not be plotted in terminal')


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
        im.save(buf, format='JPEG', quality=torch.randint(self.low, self.high,
                                                          []).item())
        im = Image.open(buf)
        im = TF.resize(im, (H, W))
        return im


def kp_normalize(H, W, kp):
    kp = kp.clone()
    kp[..., 0] = 2. * kp[..., 0] / (W - 1) - 1
    kp[..., 1] = 2. * kp[..., 1] / (H - 1) - 1
    return kp


class CelebABase(Dataset):

    def __len__(self):
        return len(self.filenames)

    def restrict_annos(self, num):
        anno_count = len(self.filenames)
        pick = np.random.choice(anno_count, num, replace=False)
        print(f"Picking annotation for images: {np.array(self.filenames)[pick].tolist()}")
        # exit(0)
        repeat = int(anno_count // num)
        self.filenames = np.tile(np.array(self.filenames)[pick], repeat)
        self.keypoints = np.tile(self.keypoints[pick], (repeat, 1, 1))


    def __getitem__(self, index):
        if (not self.use_ims and not self.use_keypoints):
            # early exit when caching is used
            return {"data": torch.zeros(3, 1, 1), "meta": {"index": index}}

        if self.use_ims:
            im = Image.open(os.path.join(self.subdir, self.filenames[index]))
        # print("imread: {:.3f}s".format(time.time() - tic)) ; tic = time.time()
        kp = None
        if self.use_keypoints:
            kp = self.keypoints[index].copy()
        meta = {}

        if self.warper is not None:
            if self.warper.returns_pairs:
                # tic = time.time()
                im1 = self.initial_transforms(im.convert("RGB"))
                # print("tx1: {:.3f}s".format(time.time() - tic)) ; tic = time.time()
                im1 = TF.to_tensor(im1) * 255
                if False:
                    from utils.visualization import norm_range
                    plt.imshow(norm_range(im1).permute(1, 2, 0).cpu().numpy())
                    plt.scatter(kp[:, 0], kp[:, 1])

                im1, im2, flow, grid, kp1, kp2 = self.warper(im1, keypts=kp, crop=self.crop)
                # print("warper: {:.3f}s".format(time.time() - tic)) ; tic = time.time()

                im1 = im1.to(torch.uint8)
                im2 = im2.to(torch.uint8)

                C, H, W = im1.shape

                im1 = TF.to_pil_image(im1)
                im2 = TF.to_pil_image(im2)

                im1 = self.transforms(im1)
                im2 = self.transforms(im2)
                # print("tx-2: {:.3f}s".format(time.time() - tic)) ; tic = time.time()

                C, H, W = im1.shape
                data = torch.stack((im1, im2), 0)
                meta = {
                    'flow': flow[0],
                    'grid': grid[0],
                    'im1': im1,
                    'im2': im2,
                    'index': index
                }
                if self.use_keypoints:
                    meta = {**meta, **{'kp1': kp1, 'kp2': kp2}}
            else:
                im1 = self.initial_transforms(im.convert("RGB"))
                im1 = TF.to_tensor(im1) * 255

                im1, kp = self.warper(im1, keypts=kp, crop=self.crop)

                im1 = im1.to(torch.uint8)
                im1 = TF.to_pil_image(im1)
                im1 = self.transforms(im1)


                C, H, W = im1.shape
                data = im1
                if self.use_keypoints:
                    meta = {
                        'keypts': kp,
                        'keypts_normalized': kp_normalize(H, W, kp),
                        'index': index
                    }

        else:
            if self.use_ims:
                data = self.transforms(self.initial_transforms(im.convert("RGB")))
                if self.crop != 0:
                    data = data[:, self.crop:-self.crop, self.crop:-self.crop]
                C, H, W = data.shape
            else:
                #  after caching descriptors, there is no point doing I/O
                H = W = self.imwidth - 2 * self.crop
                data = torch.zeros(3, 1, 1)

            if kp is not None:
                kp = kp - self.crop
                kp = torch.tensor(kp)

            if self.use_keypoints:
                meta = {
                    'keypts': kp,
                    'keypts_normalized': kp_normalize(H, W, kp),
                    'index': index
                }
        if self.visualize:
            # from torchvision.utils import make_grid
            from utils.visualization import norm_range
            num_show = 2 if self.warper and self.warper.returns_pairs else 1
            plt.clf()
            fig = plt.figure()
            for ii in range(num_show):
                im_ = data[ii] if num_show > 1 else data
                ax = fig.add_subplot(1, num_show, ii + 1)
                ax.imshow(norm_range(im_).permute(1, 2, 0).cpu().numpy())
                if self.use_keypoints:
                    if num_show == 2:
                        kp_x = meta["kp{}".format(ii + 1)][:, 0].numpy()
                        kp_y = meta["kp{}".format(ii + 1)][:, 1].numpy()
                    else:
                        kp_x = kp[:, 0].numpy()
                        kp_y = kp[:, 1].numpy()
                    ax.scatter(kp_x, kp_y)
                zs_dispFig()
                import ipdb; ipdb.set_trace()
            #     zs.
            # if self.train:
            # else:
            #     if len(data.size()) < 4:
            #         data_ = data.unsqueeze(0)
            #     else:
            #         data_ = data
            #     for im_ in data_:
            #         plt.clf()
            #         im_ = norm_range(im_).permute(1, 2, 0).cpu().numpy()
            #         plt.imshow(im_)
            #     import ipdb; ipdb.set_trace()
            # else:
            #     ims = norm_range(make_grid(data)).permute(1, 2, 0).cpu().numpy()
            #     plt.imshow(ims)
        return {"data": data, "meta": meta}


class ProfileData(Dataset):
    def __init__(self, imwidth, **kwargs):
        self.imwidth = imwidth

    def __getitem__(self, index):
        data = torch.randn(3, self.imwidth, self.imwidth)
        return {"data": data}

    def __len__(self):
        return int(1E6)


class IJBB(Dataset):
    def __init__(self, root, imwidth, prototypes, **kwargs):
        self.root = root
        self.imwidth = imwidth
        self.prototypes = prototypes
        self.im_list = sorted(glob.glob(str(Path(root) / "crop_det/*.jpg")))
        expected = 227630
        assert len(self.im_list) == expected, "expected {} images".format(expected)
        if prototypes:
            prototype_list = [
                "124171.jpg",
                "150665.jpg",
                "3128.jpg",
                "2920.jpg",
                "2782.jpg",
                "1082.jpg",
            ]
            self.im_list = [x for x in self.im_list if Path(x).name in prototype_list]

        normalize = transforms.Normalize(mean=[0.5084, 0.4224, 0.3769],
                                         std=[0.2599, 0.2371, 0.2323])
        self.initial_transforms = transforms.Compose([
            transforms.Resize(self.imwidth),
            transforms.CenterCrop(self.imwidth),
        ])
        self.transforms = transforms.Compose([transforms.ToTensor(), normalize])

    def __getitem__(self, index):
        im_path = self.im_list[index]
        im = Image.open(im_path).convert("RGB")
        data = self.transforms(self.initial_transforms(im))
        if False:
            from torchvision.utils import make_grid
            from utils.visualization import norm_range
            ims = norm_range(make_grid(data)).permute(1, 2, 0).cpu().numpy()
            plt.imshow(ims)
            import ipdb;
            ipdb.set_trace()
        return {"data": data, "im_path": im_path}

    def __len__(self):
        return len(self.im_list)


class AFLW(CelebABase):
    eye_kp_idxs = [0, 1]

    def __init__(self, root, imwidth, train, pair_warper, visualize=False, use_ims=True,
                 use_keypoints=False, do_augmentations=False, crop=0, use_minival=False,
                 **kwargs):
        self.root = root
        self.crop = crop
        self.imwidth = imwidth
        self.use_ims = use_ims
        self.visualize = visualize
        self.use_keypoints = use_keypoints
        self.use_minival = use_minival
        self.train = train
        self.warper = pair_warper

        images, keypoints, sizes = self.load_dataset(root)
        self.sizes = sizes
        self.filenames = images
        self.keypoints = keypoints.astype(np.float32)
        self.subdir = os.path.join(root, 'output')

        # print("LIMITING DATA FOR DEBGGING")
        # self.filenames = self.filenames[:1000]
        # self.keypoints = self.keypoints[:1000]
        # sizes = sizes[:1000]
        # self.sizes = sizes

        # check raw
        # im_path = pjoin(self.subdir, self.filenames[0])
        # im = Image.open(im_path).convert("RGB")
        # plt.imshow(im)
        # plt.scatter(keypoints[0, :, 0], keypoints[0, :, 1])
        self.keypoints *= self.imwidth / sizes[:, [1, 0]].reshape(-1, 1, 2)

        normalize = transforms.Normalize(mean=[0.5084, 0.4224, 0.3769],
                                         std=[0.2599, 0.2371, 0.2323])
        # NOTE: we break the aspect ratio here, but hopefully the network should
        # be fairly tolerant to this
        self.initial_transforms = transforms.Resize((self.imwidth, self.imwidth))
        augmentations = [
            JPEGNoise(),
            transforms.transforms.ColorJitter(.4, .4, .4),
            transforms.ToTensor(),
            PcaAug()
        ] if (train and do_augmentations) else [transforms.ToTensor()]
        self.transforms = transforms.Compose(augmentations + [normalize])

    def load_dataset(self, data_dir):
        # borrowed from Tom and Ankush
        if self.train or self.use_minival:
            load_subset = "train"
        else:
            load_subset = "test"
        with open(pjoin(data_dir, 'aflw_{}_images.txt'.format(load_subset)), 'r') as f:
            images = f.read().splitlines()
        mat = loadmat(os.path.join(data_dir, 'aflw_' + load_subset + '_keypoints.mat'))
        keypoints = mat['gt'][:, :, [1, 0]]
        sizes = mat['hw']

        # import ipdb; ipdb.set_trace()
        # if self.data.shape[0] == 19000:
        #     self.data = self.data[:20]

        if load_subset == 'train':
            # put the last 10 percent of the training aside for validation
            if self.use_minival:
                n_validation = int(round(0.1 * len(images)))
                if self.train:
                    images = images[:-n_validation]
                    keypoints = keypoints[:-n_validation]
                    sizes = sizes[:-n_validation]
                else:
                    images = images[-n_validation:]
                    keypoints = keypoints[-n_validation:]
                    sizes = sizes[-n_validation:]
        return images, keypoints, sizes


class Chimps(CelebABase):

    def __init__(self, root, imwidth, train, pair_warper, visualize=False,
                 use_keypoints=False, do_augmentations=False, crop=0, **kwargs):
        self.root = root
        self.crop = crop
        self.imwidth = imwidth
        self.visualize = visualize
        self.use_keypoints = use_keypoints
        self.warper = pair_warper

        subset = "train" if train else "val"
        images, keypoints, sizes = self.load_dataset(root, subset)
        self.sizes = sizes
        self.filenames = images
        self.keypoints = keypoints.astype(np.float32)
        self.subdir = root

        # check raw
        # im_path = pjoin(self.subdir, self.filenames[0])
        # im = Image.open(im_path).convert("RGB")
        # plt.imshow(im)
        # plt.scatter(keypoints[0, :, 0], keypoints[0, :, 1])
        self.keypoints *= self.imwidth / sizes[:, [1, 0]].reshape(-1, 1, 2)

        normalize = transforms.Normalize(mean=[0.5084, 0.4224, 0.3769],
                                         std=[0.2599, 0.2371, 0.2323])
        # NOTE: we break the aspect ratio here, but hopefully the network should
        # be fairly tolerant to this
        self.initial_transforms = transforms.Resize((self.imwidth, self.imwidth))
        augmentations = [
            JPEGNoise(),
            transforms.transforms.ColorJitter(.4, .4, .4),
            transforms.ToTensor(),
            PcaAug()
        ] if (train and do_augmentations) else [transforms.ToTensor()]
        self.transforms = transforms.Compose(augmentations + [normalize])

    def load_dataset(self, data_dir, subset):
        # borrowed from Tom and Ankush
        with open(pjoin(data_dir, "filelist_face_images.txt"), "r") as f:
            images = f.read().splitlines()
        mat = loadmat(os.path.join(data_dir, 'keypoint_information.mat'))
        keypoints = mat["f_keypoints"].reshape(-1, 5, 2)

        with open(pjoin(data_dir, "im_sizes.txt"), "r") as f:
            rows = [(x.split(",")) for x in f.read().splitlines()]
            sizes = np.array([[int(x[0]), int(x[1])] for x in rows])

        if subset in ['train', 'val']:
            # put the last 10 percent of the training aside for validation
            n_validation = int(round(0.1 * len(images)))
            # n_validation = 0
            if not n_validation:
                pass
            elif subset == 'train':
                images = images[:-n_validation]
                keypoints = keypoints[:-n_validation]
                sizes = sizes[:-n_validation]
            elif subset == 'val':
                images = images[-n_validation:]
                keypoints = keypoints[-n_validation:]
                sizes = sizes[-n_validation:]
            else:
                raise ValueError()
        return images, keypoints, sizes

    def __len__(self):
        return len(self.filenames)


class Helen(Dataset):
    def __init__(self, root, imwidth, train, visualize=False, thresh=0.5, rand_in=False,
                 crop2face=False, downsample_labels=0, break_preproc=False,
                 restrict_to=0, restrict_seed=0, **kwargs):
        self.root = root
        self.thresh = thresh
        self.break_preproc = break_preproc
        self.train = train
        self.visualize = visualize
        self.rand_in = rand_in
        self.imwidth = imwidth
        setlists = {
            "train": "exemplars.txt",
            "val": "tuning.txt",
            "test": "testing.txt"
        }

        self.classnames = [
            "background",
            "face-skin",  # (excluding ears and neck)
            "left-eyebrow",
            "right-eyebrow",
            "left-eye",
            "right-eye",
            "nose",
            "upper-lip",
            "inner-mouth",
            "lower-lip",
            "hair",
        ]

        im_lists = {}
        for key, val in setlists.items():
            with open(str(Path(root) / val), "r") as f:
                tokens = f.read().splitlines()
                if tokens[-1] == "":
                    tokens.pop()
                ids = [x.split(" , ")[1] for x in tokens]
                im_lists[key] = ids

        total_ims = sum([len(x) for x in im_lists.values()])
        msg = "expected {} images, found {}"
        expected = 2330
        assert total_ims == expected, msg.format(expected, total_ims)

        if restrict_to:
            np.random.seed(restrict_seed)
            num_repeats = len(im_lists["train"]) // restrict_to
            sample = np.random.choice(im_lists["train"], restrict_to)
            im_lists["train"] = sample.repeat(num_repeats)

        if train:
            self.im_list = im_lists["train"]
        else:
            self.im_list = im_lists["test"]
        normalize = transforms.Normalize(
            mean=[0.5084, 0.4224, 0.3769],
            std=[0.2599, 0.2371, 0.2323],
        )
        aug_dict = {
            "rsize": 1.05,
            "hflip": 0.5,
            "translate": (2, 2),
        }
        # "rotate": 5,
        # "hue": 0.5,
        # "gamma": 0.5,
        self.augs = get_composed_augmentations(aug_dict)
        self.resizer = transforms.Resize((self.imwidth, self.imwidth))
        self.downsample_labels = downsample_labels
        if downsample_labels:
            label_w = self.imwidth // downsample_labels
        else:
            label_w = self.imwidth
        self.label_resizer = transforms.Resize((label_w, label_w),
                                               interpolation=Image.NEAREST)
        if self.break_preproc:
            normalize = transforms.Normalize(mean=[0, 0, 0], std=[1, 1, 1])
        self.transforms = transforms.Compose([transforms.ToTensor(), normalize])
        # transforms.Resize(self.imwidth),

    def __getitem__(self, index):
        im_path = Path(self.root) / "images/{}.jpg".format(self.im_list[index])
        im = Image.open(im_path).convert("RGB")
        name = Path(im_path).stem
        anno_template = str(Path(self.root) / "labels/{}/{}_lbl{:02d}.png")
        seg = np.zeros((im.size[1], im.size[0]), dtype=np.uint8)
        # for ii in range(10, 0, -1):
        for ii in range(1, 11):
            anno_path = anno_template.format(name, name, ii)
            lbl_im = np.array(Image.open(anno_path).convert("L"))
            assert lbl_im.ndim == 2, "expected greyscale"
            # if sum(seg[lbl_im > self.thresh]) > 0:
            #     print("already colored these pixels")
            #     import ipdb; ipdb.set_trace()
            seg[lbl_im > self.thresh * 255] = ii
            # plt.matshow(seg)
            # zs_dispFig()

        seg = Image.fromarray(seg, "L")
        # if self.train and False:
        #     im, seg = self.augs(im, seg)

        seg = self.label_resizer(seg)
        seg = torch.from_numpy(np.array(seg))
        data = self.resizer(im)
        data = self.transforms(data)

        if False:
            counts = torch.histc(seg.float(), bins=11, min=0, max=10)
            probs = counts / counts.sum()
            for name, prob in zip(self.classnames, probs):
                print("{}\t {:.2f}".format(name, prob))
        if self.rand_in:
            data = torch.randn(data.shape)

        if self.visualize:
            from torchvision.utils import make_grid
            from utils.visualization import norm_range
            ims = norm_range(make_grid(data)).permute(1, 2, 0).cpu().numpy()
            plt.close("all")
            plt.axis("off")
            fig = plt.figure()  # a new figure window
            ax1 = fig.add_subplot(1, 3, 1)
            ax2 = fig.add_subplot(1, 3, 2)
            ax3 = fig.add_subplot(1, 3, 3)
            ax1.imshow(ims)
            ax2.imshow(label_colormap(seg).numpy())
            if self.downsample_labels:
                sz = tuple([x * self.downsample_labels for x in seg.size()])
                seg_ = np.array(Image.fromarray(seg.numpy()).resize(sz))
            else:
                seg_ = seg
            # ax3.matshow(seg_)
            ax3.imshow(label_colormap(seg_).numpy())
            ax3.imshow(ims, alpha=0.5)
            zs_dispFig()

        return {"data": data, "meta": {"im_path": str(im_path), "lbls": seg}}

    def __len__(self):
        return len(self.im_list)


class CelebAPrunedAligned_MAFLVal(CelebABase):
    eye_kp_idxs = [0, 1]

    def __init__(self, root, train=True, pair_warper=None, imwidth=100, crop=18,
                 do_augmentations=True, use_keypoints=False, use_hq_ims=True,
                 visualize=False, use_ims=True, val_split="celeba", val_size=2000,
                 **kwargs):
        self.root = root
        self.imwidth = imwidth
        self.train = train
        self.use_ims = use_ims
        self.warper = pair_warper
        self.visualize = visualize
        self.crop = crop
        self.use_keypoints = use_keypoints

        if use_hq_ims:
            subdir = "img_align_celeba_hq"
        else:
            subdir = "img_align_celeba"
        self.subdir = os.path.join(root, 'Img', subdir)

        anno = pd.read_csv(
            os.path.join(root, 'Anno', 'list_landmarks_align_celeba.txt'), header=1,
            delim_whitespace=True)
        assert len(anno.index) == 202599
        split = pd.read_csv(os.path.join(root, 'Eval', 'list_eval_partition.txt'),
                            header=None, delim_whitespace=True, index_col=0)
        assert len(split.index) == 202599

        mafltrain = pd.read_csv(os.path.join(root, 'MAFL', 'training.txt'), header=None,
                                delim_whitespace=True, index_col=0)
        mafltest = pd.read_csv(os.path.join(root, 'MAFL', 'testing.txt'), header=None,
                               delim_whitespace=True, index_col=0)
        # Ensure that we are not using mafl images
        split.loc[mafltrain.index] = 3
        split.loc[mafltest.index] = 4

        assert (split[1] == 4).sum() == 1000

        if train:
            self.data = anno.loc[split[split[1] == 0].index]
        elif val_split == "celeba":
            # subsample images from CelebA val, otherwise training gets slow
            self.data = anno.loc[split[split[1] == 2].index][:val_size]
        elif val_split == "mafl":
            self.data = anno.loc[split[split[1] == 4].index]

        # lefteye_x lefteye_y ; righteye_x righteye_y ; nose_x nose_y ;
        # leftmouth_x leftmouth_y ; rightmouth_x rightmouth_y
        self.keypoints = np.array(self.data, dtype=np.float32).reshape(-1, 5, 2)
        self.filenames = list(self.data.index)

        # Move head up a bit
        initial_crop = lambda im: transforms.functional.crop(im, 30, 0, 178, 178)
        self.keypoints[:, :, 1] -= 30
        self.keypoints *= self.imwidth / 178.

        normalize = transforms.Normalize(mean=[0.5084, 0.4224, 0.3769],
                                         std=[0.2599, 0.2371, 0.2323])
        augmentations = [
            JPEGNoise(),
            transforms.transforms.ColorJitter(.4, .4, .4),
            transforms.ToTensor(),
            PcaAug()
        ] if (train and do_augmentations) else [transforms.ToTensor()]

        self.initial_transforms = transforms.Compose(
            [initial_crop, transforms.Resize(self.imwidth)])
        self.transforms = transforms.Compose(augmentations + [normalize])

    def __len__(self):
        return len(self.data.index)


class MAFLAligned(CelebABase):
    eye_kp_idxs = [0, 1]

    def __init__(self, root, train=True, pair_warper=None, imwidth=100, crop=18,
                 do_augmentations=True, use_keypoints=False, use_hq_ims=True,
                 use_ims=True, visualize=False, **kwargs):
        self.root = root
        self.imwidth = imwidth
        self.use_hq_ims = use_hq_ims
        self.use_ims = use_ims
        self.visualize = visualize
        self.train = train
        self.warper = pair_warper
        self.crop = crop
        self.use_keypoints = use_keypoints
        subdir = "img_align_celeba_hq" if use_hq_ims else "img_align_celeba"
        self.subdir = os.path.join(root, 'Img', subdir)
        annos_path = os.path.join(root, 'Anno', 'list_landmarks_align_celeba.txt')
        anno = pd.read_csv(annos_path , header=1, delim_whitespace=True)

        assert len(anno.index) == 202599
        split = pd.read_csv(os.path.join(root, 'Eval', 'list_eval_partition.txt'),
                            header=None, delim_whitespace=True, index_col=0)
        assert len(split.index) == 202599
        mafltest = pd.read_csv(os.path.join(root, 'MAFL', 'testing.txt'), header=None,
                               delim_whitespace=True, index_col=0)
        split.loc[mafltest.index] = 4
        mafltrain = pd.read_csv(os.path.join(root, 'MAFL', 'training.txt'), header=None,
                                delim_whitespace=True, index_col=0)
        split.loc[mafltrain.index] = 5
        assert (split[1] == 4).sum() == 1000
        assert (split[1] == 5).sum() == 19000

        if train:
            self.data = anno.loc[split[split[1] == 5].index]
        else:
            self.data = anno.loc[split[split[1] == 4].index]

        # keypoint ordering
        # lefteye_x lefteye_y ; righteye_x righteye_y ; nose_x nose_y ;
        # leftmouth_x leftmouth_y ; rightmouth_x rightmouth_y
        self.keypoints = np.array(self.data, dtype=np.float32).reshape(-1, 5, 2)
        self.filenames = list(self.data.index)

        # Move head up a bit
        vertical_shift = 30
        crop_params = dict(i=vertical_shift, j=0, h=178, w=178)
        initial_crop = lambda im: transforms.functional.crop(im, **crop_params)
        self.keypoints[:, :, 1] -= vertical_shift
        self.keypoints *= self.imwidth / 178.
        normalize = transforms.Normalize(mean=[0.5084, 0.4224, 0.3769],
                                         std=[0.2599, 0.2371, 0.2323])
        augmentations = [
            JPEGNoise(),
            transforms.transforms.ColorJitter(.4, .4, .4),
            transforms.ToTensor(),
            PcaAug()
        ] if (train and do_augmentations) else [transforms.ToTensor()]

        self.initial_transforms = transforms.Compose(
            [initial_crop, transforms.Resize(self.imwidth)])
        self.transforms = transforms.Compose(augmentations + [normalize])


class AFLW_MTFL(CelebABase):
    """Used for testing on the 5-point version of AFLW included in the MTFL download from the
       Facial Landmark Detection by Deep Multi-task Learning (TCDCN) paper
       http://mmlab.ie.cuhk.edu.hk/projects/TCDCN.html

       For training this uses a cropped 5-point version of AFLW used in
       http://openaccess.thecvf.com/content_ICCV_2017/papers/Thewlis_Unsupervised_Learning_of_ICCV_2017_paper.pdf
       """
    eye_kp_idxs = [0, 1]

    def __init__(self, root, train=True, pair_warper=None, imwidth=70, use_ims=True,
                 crop=0, do_augmentations=True, use_keypoints=False, visualize=False, **kwargs):
        # MTFL from http://mmlab.ie.cuhk.edu.hk/projects/TCDCN/data/MTFL.zip
        self.test_root = os.path.join(root, 'MTFL')  
        # AFLW cropped from www.robots.ox.ac.uk/~jdt/aflw_10122train_cropped.zip
        self.train_root = os.path.join(root, 'aflw_cropped')  

        self.imwidth = imwidth
        self.use_ims = use_ims
        self.train = train
        self.warper = pair_warper
        self.crop = crop
        self.use_keypoints = use_keypoints
        self.visualize = visualize
        initial_crop = lambda im: im

        test_anno = pd.read_csv(os.path.join(self.test_root, 'testing.txt'),
                                header=None, delim_whitespace=True)

        if train:
            self.root = self.train_root
            all_anno = pd.read_csv(os.path.join(self.train_root, 'facedata_cropped.csv'),
                                   sep=',', header=0)
            allims = all_anno.image_file.to_list()
            trainims = all_anno[all_anno.set == 1].image_file.to_list()
            testims = [t.split('-')[-1] for t in test_anno.loc[:, 0].to_list()]

            for x in trainims:
                assert x not in testims

            for x in testims:
                assert x in allims

            self.filenames = all_anno[all_anno.set == 1].crop_file.to_list()
            self.keypoints = np.array(all_anno[all_anno.set == 1].iloc[:, 4:14],
                                      dtype=np.float32).reshape(-1, 5, 2)

            self.keypoints -= 1  # matlab to python
            self.keypoints *= self.imwidth / 150.

            assert len(self.filenames) == 10122
        else:
            self.root = self.test_root
            keypoints = np.array(test_anno.iloc[:, 1:11], dtype=np.float32)
            self.keypoints = keypoints.reshape(-1, 2, 5).transpose(0, 2, 1)
            self.filenames = test_anno[0].to_list()

            self.keypoints -= 1  # matlab to python
            self.keypoints *= self.imwidth / 150.

            assert len(self.filenames) == 2995
        self.subdir = self.root

        # print("HARDCODING DEBGGER")
        # self.filenames = self.filenames[:100]
        # self.keypoints = self.keypoints[:100]

        normalize = transforms.Normalize(mean=[0.5084, 0.4224, 0.3769],
                                         std=[0.2599, 0.2371, 0.2323])
        augmentations = [
            JPEGNoise(),
            transforms.transforms.ColorJitter(.4, .4, .4),
            transforms.ToTensor(),
            PcaAug()
        ] if (train and do_augmentations) else [transforms.ToTensor()]
        self.initial_transforms = transforms.Compose(
            [initial_crop, transforms.Resize(self.imwidth)])
        self.transforms = transforms.Compose(augmentations + [normalize])


class ThreeHundredW(Dataset):
    """The 300W dataset, which is an amalgamation of several other datasets

    We use the split from "Face alignment at 3000 fps via regressing local binary features"
    Where they state:
    "Our training set consists of AFW, the training sets of LFPW,
    and the training sets of Helen,  with 3148 images in total.
    Our testing set consists of IBUG, the testing sets of LFPW,
    and the testing sets of Helen, with 689 images in total.
    We do not use images from XM2VTS as it is taken under a
    controlled environment and is too simple"
    """
    eye_kp_idxs = [36, 45]

    def __init__(self, root, train=True, pair_warper=None, imwidth=100, use_ims=True,
                 crop=15, do_augmentations=True, use_keypoints=False, visualize=False, **kwargs):
        from scipy.io import loadmat

        self.root = root
        self.imwidth = imwidth
        self.train = train
        self.warper = pair_warper
        self.crop = crop
        self.use_ims = use_ims
        self.use_keypoints = use_keypoints
        self.visualize = visualize

        afw = loadmat(os.path.join(root, 'Bounding Boxes/bounding_boxes_afw.mat'))
        helentr = loadmat(os.path.join(root, 'Bounding Boxes/bounding_boxes_helen_trainset.mat'))
        helente = loadmat(os.path.join(root, 'Bounding Boxes/bounding_boxes_helen_testset.mat'))
        lfpwtr = loadmat(os.path.join(root, 'Bounding Boxes/bounding_boxes_lfpw_trainset.mat'))
        lfpwte = loadmat(os.path.join(root, 'Bounding Boxes/bounding_boxes_lfpw_testset.mat'))
        ibug = loadmat(os.path.join(root, 'Bounding Boxes/bounding_boxes_ibug.mat'))

        self.filenames = []
        self.bounding_boxes = []
        self.keypoints = []

        if train:
            datasets = [(afw, 'afw'), (helentr, 'helen/trainset'), (lfpwtr, 'lfpw/trainset')]
        else:
            datasets = [(helente, 'helen/testset'), (lfpwte, 'lfpw/testset'), (ibug, 'ibug')]

        for dset in datasets:
            ds = dset[0]
            ds_imroot = dset[1]
            imnames = [ds['bounding_boxes'][0, i]['imgName'][0, 0][0] for i in range(ds['bounding_boxes'].shape[1])]
            bbs = [ds['bounding_boxes'][0, i]['bb_ground_truth'][0, 0][0] for i in range(ds['bounding_boxes'].shape[1])]

            for i, imn in enumerate(imnames):
                # only some of the images given in ibug boxes exist (those that start with 'image')
                if ds is not ibug or imn.startswith('image'):
                    self.filenames.append(os.path.join(ds_imroot, imn))
                    self.bounding_boxes.append(bbs[i])

                    kpfile = os.path.join(root, ds_imroot, imn[:-3] + 'pts')
                    with open(kpfile) as kpf:
                        kp = kpf.read()
                    kp = kp.split()[5:-1]
                    kp = [float(k) for k in kp]
                    assert len(kp) == 68 * 2
                    kp = np.array(kp).astype(np.float32).reshape(-1, 2)
                    self.keypoints.append(kp)

        if train:
            assert len(self.filenames) == 3148
        else:
            assert len(self.filenames) == 689

        normalize = transforms.Normalize(mean=[0.5084, 0.4224, 0.3769], std=[0.2599, 0.2371, 0.2323])
        augmentations = [JPEGNoise(), transforms.transforms.ColorJitter(.4, .4, .4),
                         transforms.ToTensor(), PcaAug()] if (train and do_augmentations) else [transforms.ToTensor()]

        self.initial_transforms = transforms.Compose([transforms.Resize(self.imwidth)])
        self.transforms = transforms.Compose(augmentations + [normalize])

        # print("HARDCODING DEBGGER")
        # self.filenames = self.filenames[:100]
        # self.keypoints = self.keypoints[:100]

    def __len__(self):
        return len(self.filenames)

    def __getitem__(self, index):
        if self.use_ims:
            im = Image.open(os.path.join(self.root, self.filenames[index])).convert("RGB")
        # Crop bounding box
        xmin, ymin, xmax, ymax = self.bounding_boxes[index]
        keypts = self.keypoints[index]

        # This is basically copied from matlab code and assumes matlab indexing
        bw = xmax - xmin + 1
        bh = ymax - ymin + 1
        bcy = ymin + (bh + 1) / 2
        bcx = xmin + (bw + 1) / 2

        # To simplify the preprocessing, we do two image resizes (can fix later if speed
        # is an issue)
        preresize_sz = 100

        bw_ = 52  # make the (tightly cropped) face 52px
        fac = bw_ / bw
        if self.use_ims:
            imr = im.resize((int(im.width * fac), int(im.height * fac)))

        bcx_ = int(np.floor(fac * bcx))
        bcy_ = int(np.floor(fac * bcy))
        bx = bcx_ - bw_ / 2 + 1
        bX = bcx_ + bw_ / 2
        by = bcy_ - bw_ / 2 + 1
        bY = bcy_ + bw_ / 2
        pp = (preresize_sz - bw_) / 2
        bx = int(bx - pp)
        bX = int(bX + pp)
        by = int(by - pp - 2)
        bY = int(bY + pp - 2)

        if self.use_ims:
            imr = pad_and_crop(np.array(imr), [(by - 1), bY, (bx - 1), bX])
            im = Image.fromarray(imr)

        cutl = bx - 1
        keypts = keypts.copy() * fac
        keypts[:, 0] = keypts[:, 0] - cutl
        cutt = by - 1
        keypts[:, 1] = keypts[:, 1] - cutt

        kp = None
        if self.use_keypoints:
            kp = keypts - 1  # from matlab to python style
            kp = kp * self.imwidth / preresize_sz
            kp = torch.tensor(kp)
        meta = {}

        if self.warper is not None:
            if self.warper.returns_pairs:
                im1 = self.initial_transforms(im.convert("RGB"))
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
                im1 = self.initial_transforms(im.convert("RGB"))
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
            if self.use_ims:
                data = self.transforms(self.initial_transforms(im.convert("RGB")))
                if self.crop != 0:
                    data = data[:, self.crop:-self.crop, self.crop:-self.crop]
                C, H, W = data.shape
            else:
                # after caching descriptors, there is no point doing I/O
                H = W = self.imwidth - 2 * self.crop
                data = torch.zeros(3, 1, 1)

            if kp is not None:
                kp = kp - self.crop
                kp = torch.tensor(kp)

            if self.use_keypoints:
                meta = {'keypts': kp, 'keypts_normalized': kp_normalize(H, W, kp), 'index': index}

        if self.visualize:
            from utils.visualization import norm_range
            num_show = 2 if self.warper and self.warper.returns_pairs else 1
            fig = plt.figure()
            for ii in range(num_show):
                im_ = data[ii] if num_show > 1 else data
                ax = fig.add_subplot(1, num_show, ii + 1)
                ax.imshow(norm_range(im_).permute(1, 2, 0).cpu().numpy())
                if self.use_keypoints:
                    if num_show == 2:
                        kp_x = meta["kp{}".format(ii + 1)][:, 0].numpy()
                        kp_y = meta["kp{}".format(ii + 1)][:, 1].numpy()
                    else:
                        kp_x = kp[:, 0].numpy()
                        kp_y = kp[:, 1].numpy()
                    ax.scatter(kp_x, kp_y)
            import ipdb; ipdb.set_trace()

        return {"data": data, "meta": meta}


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="Helen")
    parser.add_argument("--train", action="store_true")
    parser.add_argument("--use_keypoints", action="store_true")
    parser.add_argument("--use_ims", type=int, default=1)
    parser.add_argument("--use_minival", action="store_true")
    parser.add_argument("--break_preproc", action="store_true")
    parser.add_argument("--pairs", action="store_true")
    parser.add_argument("--rand_in", action="store_true")
    parser.add_argument("--restrict_to", type=int, help="restrict to n images")
    parser.add_argument("--downsample_labels", type=int, default=2)
    parser.add_argument("--show", type=int, default=2)
    parser.add_argument("--restrict_seed", type=int, default=0)
    parser.add_argument("--root")
    args = parser.parse_args()

    default_roots = {
        "CelebAPrunedAligned_MAFLVal": "data/celeba",
        "MAFLAligned": "data/celeba",
        "AFLW_MTFL": "data/aflw-mtfl",
        "Helen": "data/SmithCVPR2013_dataset_resized",
        "AFLW": "data/aflw/aflw_release-2",
        "ThreeHundredW": "data/300w/300w",
        "Chimps": "data/chimpanzee_faces/datasets_cropped_chimpanzee_faces/data_CZoo",
    }
    root = default_roots[args.dataset] if args.root is None else args.root

    imwidth = 136
    kwargs = {
        "root": root,
        "train": args.train,
        "use_keypoints": args.use_keypoints,
        "use_ims": args.use_ims,
        "visualize": True,
        "use_minival": args.use_minival,
        "downsample_labels": args.downsample_labels,
        "break_preproc": args.break_preproc,
        "rand_in": args.rand_in,
        "restrict_to": args.restrict_to,
        "restrict_seed": args.restrict_seed,
        "imwidth": imwidth,
        "crop": 20,
    }
    if args.train and args.pairs:
        warper = tps.Warper(H=imwidth, W=imwidth)
    elif args.train:
        warper = tps.WarperSingle(H=imwidth, W=imwidth)
    else:
        warper = None
    kwargs["pair_warper"] = warper

    show = args.show
    if args.restrict_to:
        show = min(args.restrict_to, show)
    if args.dataset == "IJBB":
        dataset = IJBB('data/ijbb', prototypes=True, imwidth=128, train=False)
        for ii in range(show):
            dataset[ii]
    else:
        dataset = globals()[args.dataset](**kwargs)
        for ii in range(show):
            dataset[ii]
