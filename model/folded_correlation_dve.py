import torch
import argparse
import torch.nn.functional as F
import copy
from utils import tps
import time
from collections import defaultdict
from torch.autograd import gradcheck

PROFILE = False
PRINT_MEM = False
OLD_METHOD = False
LOCAL_CHECKS = 0
LOCAL_CHECKS_INNER_LOOP = 0

# NOTE: To pass numerical tests with double precision, this value needs to
# be mega low, but for single precision machine-epsilon is around 2**(-23), so
# we need some buffer to help avoid division by zero errors.
EPS = 1E-8

# Risk appetite
ATOL = 1E-4

# NOTE: Without a very high JDT factor, the numerical tests will not pass for
# a large EVC dimension (e.g. 1E3)
# JDT_FACTOR = 1E3
JDT_FACTOR = 20


def estimate_mem(x):
    if x.dtype == torch.float64:
        nbytes = 8
    elif x.dtype == torch.float32:
        nbytes = 4
    elif x.dtype == torch.int32:
        nbytes = 4
    elif x.dtype == torch.float16:
        nbytes = 2
    elif x.dtype == torch.int8:
        nbytes = 1
    else:
        import ipdb; ipdb.set_trace()
    return torch.numel(x) * nbytes / (1024) ** 3


class DenseCorrDve(torch.autograd.Function):

    @staticmethod
    def forward(ctx, feats1, feats2, xxyy, batch_grid_u, stride, norm, pow=0.5):
        """Compute the folded dense correlation loss forward pass.

        Args:
            feats1 (torch.Tensor): N x C x h x h tensor of features
            feats2 (torch.Tensor): N x C x h x w tensor of features
            xxyy (torch.Tensor): H x W x 2 grid of uniform sampling locations.
            batch_grid_u (torch.Tensor): N x h x w x 2 grid of sampling
                locations.
            stride (int): the stride to be applied to the image grid to match
                the spatial dimensions of the features (so that
                `H = h * stride`).
            norm (bool): whether to remove normalisation.
            pow (float :: 0.5): power by which to raise the root distances
                between pixel locations.

        Returns:
            (torch.Tensor): The total loss for the given minibatch of inputs.
        """
        with torch.no_grad():
            B, C, H, W = feats1.shape
            h, w = H, W
            params = torch.IntTensor([B, C, H, W, stride, norm])
            pow_tensor = torch.FloatTensor([pow])
            ctx.save_for_backward(feats1, feats2, xxyy, batch_grid_u,
                                  params, pow_tensor)

            loss = 0.
            for b in range(B):
                f1 = feats1[b].reshape(C, H * W)  # source
                f2 = feats2[b].reshape(C, h * w)  # target
                fa = feats1[(b + 1) % B].reshape(C, h * w)  # auxiliary

                if norm:
                    f1 = F.normalize(f1, p=2, dim=0) * JDT_FACTOR
                    f2 = F.normalize(f2, p=2, dim=0) * JDT_FACTOR
                    fa = F.normalize(fa, p=2, dim=0) * JDT_FACTOR

                corr = torch.matmul(f1.t(), fa)
                corr = corr.reshape(H, W, h, w)
                smcorr = F.softmax(corr.reshape(H, W, -1), dim=2).reshape(corr.shape)
                smcorr_fa = smcorr[None, ...] * fa.reshape(-1, 1, 1, h, w)
                del smcorr

                f1_via_fa = smcorr_fa.sum((3, 4)).reshape(C, H * w)
                del smcorr_fa

                corr2 = torch.matmul(f1_via_fa.t(), f2).reshape(corr.shape)
                smcorr2 = F.softmax(corr2.reshape(H, W, -1), dim=2).reshape(corr.shape)
                del corr2

                with torch.no_grad():
                    diff = batch_grid_u[b, :, :, None, None, :] - \
                            xxyy[None, None, ::stride, ::stride, :]
                    diff = (diff * diff).sum(4).sqrt()
                    diff = diff.pow(pow)

                L = diff * smcorr2

                loss += L.float().sum()
        return loss / (H * W * B)

    @staticmethod
    def backward(ctx, grad_output):
        """Compute the folded dense correlation loss (with EVC) backward pass.

        Args:
            (torch.Tensor): The gradient of the total loss with respect to the
                output of the dense correlation loss.

        Returns:
            (torch.Tensor): N x C x h x h tensor of gradients
            (torch.Tensor): N x C x h x w tensor of gradients
            (None): H x W x 2 grid of uniform sampling locations
            (None): no gradient for `xxyy`
            (None): no gradient for `batch_grid_u`
            (None): no gradient for `stride`
            (None): no gradient for `pow`
        """
        if PROFILE:
            batch_tic = time.time()
            tic = time.time()
            timings = defaultdict(float)

        feats1, feats2, xxyy, batch_grid_u, params, pow = ctx.saved_tensors

        """We needed to store the integers as part of a tensor, so the
        unpacking code here is a little convoluted."""
        B, C, H, W, stride, norm = [x.item() for x in params]
        h, w = H, W
        pow = pow.item()

        """This is a pattern that is very convenient - at the top of backward
        unpack saved_tensors and initialize all gradients w.r.t. inputs to
        None. Thanks to the fact that additional trailing Nones are
        ignored, the return statement is simple even when the function has
        optional inputs."""
        grad_feats1 = grad_feats2 = grad_xxyy = grad_batch_u = None
        grad_stride = grad_norm = grad_pow = None

        """Returning gradients for inputs that don't require it is
        not an error."""
        assert ctx.needs_input_grad[0], "expected feats1 to need grad"
        assert ctx.needs_input_grad[1], "expected feats2 to need grad"
        assert not ctx.needs_input_grad[2], "expected xxyy does not need grad"
        assert not ctx.needs_input_grad[3], "expected batch_grid_u does not need grad"
        assert not ctx.needs_input_grad[4], "expected stride does not need grad"

        if PROFILE:
            timings["back-init"] = time.time() - tic
            tic = time.time()

        with torch.no_grad():

            if feats1.is_cuda:
                # TODO: clean up types here
                if feats1.dtype == torch.float32:
                    grad_feats1 = torch.cuda.FloatTensor(B, C, H, W).fill_(0)
                    grad_feats2 = torch.cuda.FloatTensor(B, C, h, w).fill_(0)
                elif feats1.dtype == torch.float16:
                    grad_feats1 = torch.cuda.HalfTensor(B, C, H, W).fill_(0)
                    grad_feats2 = torch.cuda.HalfTensor(B, C, h, w).fill_(0)
            else:
                grad_feats1 = torch.zeros((B, C, H, W), dtype=feats1.dtype)
                grad_feats2 = torch.zeros((B, C, h, w), dtype=feats2.dtype)

            grad_loss = grad_output / (H * W * B)

            if PROFILE:
                timings["data transfer"] = time.time() - batch_tic

            for b in range(B):

                if PROFILE:
                    tic = time.time()

                with torch.no_grad():
                    diff = batch_grid_u[b, :, :, None, None, :] - \
                           xxyy[None, None, ::stride, ::stride, :]
                    diff = (diff * diff).sum(4).sqrt()
                    diff = diff.pow(pow)

                if PROFILE:
                    timings["diff-grid"] += time.time() - tic
                    tic = time.time()

                # loss gradient for the current minibatch element (expand to tensor)
                grad_loss_b = grad_loss
                grad_smcorr2 = grad_loss_b * diff

                if LOCAL_CHECKS:
                    ones = torch.ones(diff.shape, dtype=diff.dtype)
                    grad_loss_b_ = ones * grad_loss
                    smcorr_ = torch.randn(
                        diff.shape,
                        dtype=torch.double,
                        requires_grad=True)
                    with torch.autograd.enable_grad():
                        L_ = diff * smcorr_
                        d_smcorr = torch.autograd.grad(
                            outputs=L_,
                            inputs=smcorr_,
                            grad_outputs=grad_loss_b_,
                        )
                        rel_diff(grad_smcorr2, d_smcorr[0], "smax")
                        if torch.any(torch.isnan(grad_smcorr2[0])):
                            import ipdb; ipdb.set_trace()


                if PROFILE:
                    timings["scale-feats"] += time.time() - tic
                    tic = time.time()

                # Re-compute intermediate values
                grad_smcorr2 = grad_smcorr2.view(H, W, -1)
                f1_ = feats1[b].view(C, H * W)
                f2_ = feats2[b].view(C, h * w)
                fa_ = feats1[(b + 1) % B].reshape(C, h * w)  # auxiliary

                if norm:
                    f1_norm = F.normalize(f1_, p=2, dim=0) * JDT_FACTOR
                    f2_norm = F.normalize(f2_, p=2, dim=0) * JDT_FACTOR
                    fa_norm = F.normalize(fa_, p=2, dim=0) * JDT_FACTOR
                else:
                    f1_norm = f1_.clone()
                    f2_norm = f2_.clone()
                    fa_norm = fa_.clone()

                if PROFILE:
                    timings["fwd-norm"] += time.time() - tic
                    tic = time.time()

                # Match the source features against the auxiliaries
                corr = torch.matmul(f1_norm.t(), fa_norm)
                corr = corr.reshape(H, W, h, w)

                if PROFILE:
                    timings["f1-aux-correlation"] += time.time() - tic
                    tic = time.time()

                smcorr = F.softmax(corr.view(H, W, -1), dim=2)
                smcorr = smcorr.view(corr.shape)
                if LOCAL_CHECKS:
                    # cache a copy of the mega tensor for numerical checks
                    smcorr_fa = smcorr[None, ...] * fa_norm.view(-1, 1, 1, h, w)
                    f1_via_fa = smcorr_fa.sum((3, 4))
                else:
                    """This is one of the largest tensors....."""
                    f1_via_fa = (smcorr[None, ...] *
                        fa_norm.view(-1, 1, 1, h, w)).sum((3, 4))

                f1_via_fa = f1_via_fa.view(C, H * W)

                # Main correlation computation
                corr2 = torch.matmul(f1_via_fa.t(), f2_norm).view(corr.shape)

                # Direct backward pass for second softmax
                smcorr2 = F.softmax(corr2.view(H, W, -1), dim=2)
                sum_term = torch.sum(grad_smcorr2 * smcorr2, dim=2, keepdim=True)
                grad_corr2 = smcorr2 * (grad_smcorr2 - sum_term)

                if not LOCAL_CHECKS:
                    del smcorr2

                if PROFILE:
                    timings["softmax"] += time.time() - tic
                    tic = time.time()

                # safety checks
                if LOCAL_CHECKS:
                    with torch.enable_grad():
                        corr2_num = corr2.clone().requires_grad_()
                        corr2_num = corr2_num.reshape(H, W, -1)
                        smcorr2_num = F.softmax(corr2_num, dim=2)
                        grad_corr2_num = torch.autograd.grad(
                            outputs=smcorr2_num,
                            inputs=(corr2_num,),
                            grad_outputs=grad_smcorr2,
                        )
                        rel_diff(grad_corr2, grad_corr2_num[0], "smax-corr2")

                """Derivatives through the main correlation correlation"""
                grad_corr2 = grad_corr2.view(H * W, H * W)
                grad_f1_via_fa = torch.matmul(grad_corr2, f2_norm.t()).t()
                grad_f2_norm = torch.matmul(f1_via_fa, grad_corr2)

                if not LOCAL_CHECKS:
                    del grad_corr2

                if PROFILE:
                    timings["corr-back"] += time.time() - tic
                    tic = time.time()

                if LOCAL_CHECKS:
                    with torch.enable_grad():
                        f1_via_fa_num = f1_via_fa.clone().requires_grad_()
                        f2_norm_num = f2_norm.clone().requires_grad_()
                        corr2_num = torch.matmul(f1_via_fa_num.t(), f2_norm_num)
                        grad_f1_via_fa_num, grad_f2_norm_num = torch.autograd.grad(
                            outputs=corr2_num,
                            inputs=(f1_via_fa_num, f2_norm_num),
                            grad_outputs=grad_corr2,
                        )
                        rel_diff(grad_f1_via_fa, grad_f1_via_fa_num,
                                 "corr-f1-via-fa")
                        rel_diff(grad_f2_norm, grad_f2_norm_num,
                                "corr->f2-norm")

                if OLD_METHOD:
                    # (may be able to collapse all this later)
                    grad_f1_via_fa = grad_f1_via_fa.view(-1, H, W, 1, 1)

                    # This tensor is crashing the GPU
                    grad_smcorr_fa = grad_f1_via_fa.repeat(1, 1, 1, h, w)

                    # safety checks over the summation
                    if LOCAL_CHECKS:
                        with torch.enable_grad():

                            smcorr_fa_num = smcorr_fa.clone().requires_grad_()
                            f1_via_fa_num = smcorr_fa_num.sum((3, 4))
                            # f1_via_fa_num = f1_via_fa_num.view(C, H * W)

                            grad_smcorr_fa_num = torch.autograd.grad(
                                outputs=f1_via_fa_num,
                                inputs=(smcorr_fa_num,),
                                grad_outputs=grad_f1_via_fa.view(-1, H, w),
                            )
                            rel_diff(grad_smcorr_fa, grad_smcorr_fa_num[0],
                                     "summation of grad_smcorr-fa")

                    # smcorr_fa = smcorr[None, ...] * fa_.view(-1, 1, 1, h, w)
                    grad_smcorr = (grad_smcorr_fa * fa_norm.view(-1, 1, 1, h, w)).sum(0)
                    grad_fa_ = (grad_smcorr_fa * smcorr[None, ...]).sum(1).sum(1)
                    grad_fa_ = grad_fa_.reshape(C, h * w)

                    # safety checks over the weighted sum
                    if LOCAL_CHECKS:
                        with torch.enable_grad():

                            smcorr_num = smcorr.clone().requires_grad_()
                            fa_norm_num = fa_norm.clone().requires_grad_()
                            smcorr_fa_num = smcorr_num[None, ...] \
                                    * fa_norm_num.view(-1, 1, 1, h, w)

                            (grad_smcorr_num, grad_fa_num) = torch.autograd.grad(
                                outputs=smcorr_fa_num,
                                inputs=(smcorr_num, fa_norm_num),
                                grad_outputs=grad_smcorr_fa,
                            )
                            rel_diff(grad_fa_, grad_fa_num,
                                     "product of grad_fa_")
                            rel_diff(grad_smcorr, grad_smcorr_num,
                                     "product of grad_smcor")
                else:
                    # -------------------------------------------------------
                    # Collapsed summation method
                    # -------------------------------------------------------
                    # Fwd ops ->
                    # smcorr_fa = smcorr[None, ...] * fa.reshape(-1, 1, 1, h, w)
                    # f1_via_fa = smcorr_fa.sum((3, 4)).reshape(C, H * w)

                    # Given gradient ->
                    # (grad_f1_via_fa)

                    # Desired gradients ->
                    # (grad_fa_, grad_smcorr)

                    grad_f1_via_fa = grad_f1_via_fa.view(-1, H, W, 1, 1)

                    # safety checks over the summation
                    if LOCAL_CHECKS:
                        # This tensor is crashing the GPU, so should only be
                        # used for numerical checks
                        grad_smcorr_fa = grad_f1_via_fa.repeat(1, 1, 1, h, w)
                        with torch.enable_grad():

                            smcorr_fa_num = smcorr_fa.clone().requires_grad_()
                            f1_via_fa_num = smcorr_fa_num.sum((3, 4))
                            # f1_via_fa_num = f1_via_fa_num.view(C, H * W)

                            grad_smcorr_fa_num = torch.autograd.grad(
                                outputs=f1_via_fa_num,
                                inputs=(smcorr_fa_num,),
                                grad_outputs=grad_f1_via_fa.view(-1, H, w),
                            )
                            rel_diff(grad_smcorr_fa, grad_smcorr_fa_num[0],
                                     "summation of grad_smcorr-fa")

                    # Use for-loop over EVC dimension to avoid memory issues
                    if feats1.is_cuda:
                        if grad_f1_via_fa.dtype == torch.float64:
                            grad_smcorr =  torch.cuda.DoubleTensor(H, W, h, w).fill_(0)
                            grad_fa_ =  torch.cuda.DoubleTensor(C, h, w).fill_(0)
                        else:
                            grad_smcorr =  torch.cuda.FloatTensor(H, W, h, w).fill_(0)
                            grad_fa_ =  torch.cuda.FloatTensor(C, h, w).fill_(0)
                    else:
                        grad_smcorr =  torch.zeros((H, W, h, w), dtype=feats1.dtype)
                        grad_fa_ =  torch.zeros((C, h, w), dtype=feats1.dtype)

                    for cc in range(C):
                        grad_smcorr += (grad_f1_via_fa[cc] * fa_norm[cc].view(1, 1, h, w))
                        grad_fa_[cc] = (grad_f1_via_fa[cc] * smcorr).sum((0, 1))
                    grad_fa_ = grad_fa_.reshape(C, h * w)

                    # safety checks over the weighted sum
                    if LOCAL_CHECKS:
                        with torch.enable_grad():

                            smcorr_num = smcorr.clone().requires_grad_()
                            fa_norm_num = fa_norm.clone().requires_grad_()
                            smcorr_fa_num = smcorr_num[None, ...] \
                                    * fa_norm_num.view(-1, 1, 1, h, w)

                            (grad_smcorr_num, grad_fa_num) = torch.autograd.grad(
                                outputs=smcorr_fa_num,
                                inputs=(smcorr_num, fa_norm_num),
                                grad_outputs=grad_smcorr_fa,
                            )
                            rel_diff(grad_fa_, grad_fa_num,
                                     "product of grad_fa_")
                            rel_diff(grad_smcorr, grad_smcorr_num,
                                     "product of grad_smcor")

                    if PRINT_MEM:
                        key = None
                        val = None
                        shape_mems = {}
                        for key, val in locals().items():
                            if hasattr(val, "shape"):
                                shape_mems[key] = estimate_mem(val)

                        sorted_mems = sorted(shape_mems.items(), key=lambda kv: -kv[1])
                        for key, val in sorted_mems:
                            print("{}: {:.4f} GiB".format(key, val))

                # Direct backward pass for first softmax
                # smcorr = F.softmax(corr.view(H, W, -1), dim=2)
                grad_smcorr = grad_smcorr.view(H, W, -1)
                smcorr = smcorr.view(H, W, -1)
                sum_term = torch.sum(grad_smcorr * smcorr, dim=2, keepdim=True)
                grad_corr = smcorr * (grad_smcorr - sum_term)

                if not LOCAL_CHECKS:
                    del grad_smcorr
                    del grad_smcorr2
                    del smcorr
                    del corr

                if LOCAL_CHECKS:
                    with torch.enable_grad():
                        corr_num = corr.clone().requires_grad_()
                        smcorr_num = F.softmax(corr_num.view(H, W, -1), dim=2)
                        smcorr_num = smcorr_num.reshape(corr_num.shape)
                        grad_corr_num = torch.autograd.grad(
                            outputs=smcorr_num,
                            inputs=(corr_num,),
                            grad_outputs=grad_smcorr.view(H, W, h, w),
                        )
                        rel_diff(grad_corr, grad_corr_num[0].view(H, W, -1),
                                 "smax-corr")

                # Back through the first correlation
                # [Fwd op] -> `corr = torch.matmul(f1_norm.t(), fa_norm)`
                grad_corr = grad_corr.view(H * W, h * w)
                grad_f1_norm = torch.matmul(grad_corr, fa_norm.t()).t()
                grad_fa_norm = torch.matmul(f1_norm, grad_corr)

                if not LOCAL_CHECKS:
                    del grad_corr


                if LOCAL_CHECKS:
                    with torch.enable_grad():
                        f1_norm_num = f1_norm.clone().requires_grad_()
                        fa_norm_num = fa_norm.clone().requires_grad_()
                        corr_num = torch.matmul(f1_norm_num.t(), fa_norm_num)
                        grad_f1_norm_num, grad_fa_norm_num = torch.autograd.grad(
                            outputs=corr_num,
                            inputs=(f1_norm_num, fa_norm_num),
                            grad_outputs=grad_corr,
                        )
                        rel_diff(grad_f1_norm, grad_f1_norm_num, "corr->f1n-orm")
                        rel_diff(grad_fa_norm, grad_fa_norm_num, "corr->fa-norm")

                # Combine gradients for two ops using aux features
                grad_fa_norm = grad_fa_norm + grad_fa_

                # Back through the norms
                # [Fwd op] -> `f1_norm = F.normalize(f1_, p=2, dim=0) * JDT_FACTOR`
                # [Fwd op] -> `f2_norm = F.normalize(f2_, p=2, dim=0) * JDT_FACTOR`
                # [Fwd op] -> `fa_norm = F.normalize(fa_, p=2, dim=0) * JDT_FACTOR`
                # xNorm = sqrt(sum(x.*x, 3) + opts.epsilon) ;

                if norm:
                    f1_norm_val = torch.norm(f1_, p=2, dim=0).clamp(min=EPS)
                    f2_norm_val = torch.norm(f2_, p=2, dim=0).clamp(min=EPS)
                    fa_norm_val = torch.norm(fa_, p=2, dim=0).clamp(min=EPS)

                    max_val_f1 = torch.max(f1_norm_val)
                    max_val_f2 = torch.max(f2_norm_val)
                    max_val_fa = torch.max(fa_norm_val)
                    if max_val_f1 + max_val_f2 + max_val_fa > 1E8:
                        import ipdb; ipdb.set_trace()

                    grad_f1_norm_ = grad_f1_norm / f1_norm_val
                    grad_f1 = JDT_FACTOR * (grad_f1_norm_ -
                      (grad_f1_norm_ * f1_).sum(0) * (f1_ / (f1_norm_val ** 2)))

                    grad_f2_norm_ = grad_f2_norm / f2_norm_val
                    grad_f2 = JDT_FACTOR * (grad_f2_norm_ -
                       (grad_f2_norm_ * f2_).sum(0) * (f2_ / (f2_norm_val ** 2)))

                    grad_fa_norm_ = grad_fa_norm / fa_norm_val
                    grad_fa = JDT_FACTOR * (grad_fa_norm_ -
                      (grad_fa_norm_ * fa_).sum(0) * (fa_ / (fa_norm_val ** 2)))

                    if LOCAL_CHECKS:
                        with torch.enable_grad():
                            f1_num = f1_.clone().requires_grad_()
                            f2_num = f2_.clone().requires_grad_()
                            fa_num = fa_.clone().requires_grad_()

                            f1_norm_num = F.normalize(f1_num, p=2, dim=0) * JDT_FACTOR
                            f2_norm_num = F.normalize(f2_num, p=2, dim=0) * JDT_FACTOR
                            fa_norm_num = F.normalize(fa_num, p=2, dim=0) * JDT_FACTOR

                            grad_f1_num = torch.autograd.grad(
                                outputs=f1_norm_num,
                                inputs=(f1_num,),
                                grad_outputs=grad_f1_norm,
                            )
                            grad_f2_num = torch.autograd.grad(
                                outputs=f2_norm_num,
                                inputs=(f2_num,),
                                grad_outputs=grad_f2_norm,
                            )
                            grad_fa_num = torch.autograd.grad(
                                outputs=fa_norm_num,
                                inputs=(fa_num,),
                                grad_outputs=grad_fa_norm,
                            )
                            rel_diff(grad_f1, grad_f1_num[0], "norm-f1")
                            rel_diff(grad_f2, grad_f2_num[0], "norm-f2")
                            rel_diff(grad_fa, grad_fa_num[0], "norm-fa")
                else:
                    grad_f1 = grad_f1_norm
                    grad_f2 = grad_f2_norm
                    grad_fa = grad_fa_norm


                if PRINT_MEM:
                    key = None
                    val = None
                    shape_mems = {}
                    print("=======================")
                    for key, val in locals().items():
                        if hasattr(val, "shape"):
                            shape_mems[key] = estimate_mem(val)

                    sorted_mems = sorted(shape_mems.items(), key=lambda kv: -kv[1])
                    for key, val in sorted_mems:
                        print("{}: {:.4f} GiB".format(key, val))
                    import ipdb; ipdb.set_trace()


                # safety checks over the whole inner loop
                if LOCAL_CHECKS:
                    with torch.enable_grad():

                        f1_num = feats1[b].clone().detach().requires_grad_().reshape(C, H * W)
                        f2_num = feats2[b].clone().detach().requires_grad_().reshape(C, h * w)
                        fa_num = feats1[(b + 1) % B].clone().detach().requires_grad_().reshape(C, h * w)

                        if norm:
                            f1_norm_num = F.normalize(f1_num, p=2, dim=0) * JDT_FACTOR
                            f2_norm_num = F.normalize(f2_num, p=2, dim=0) * JDT_FACTOR
                            fa_norm_num = F.normalize(fa_num, p=2, dim=0) * JDT_FACTOR
                        else:
                            f1_norm_num = f1_num
                            f2_norm_num = f2_num
                            fa_norm_num = fa_num

                        # BLock 1 ------------------------------------------
                        corr_num = torch.matmul(f1_norm_num.t(), fa_norm_num)
                        corr_num = corr_num.reshape(H, W, H, W)
                        smcorr_num = F.softmax(corr_num.reshape(H, W, -1), dim=2)
                        smcorr_num = smcorr_num.reshape(corr_num.shape)
                        # BLock 1 ------------------------------------------


                        # BLock 2 ------------------------------------------
                        smcorr_fa_num = smcorr_num[None, ...] * \
                                fa_norm_num.reshape(-1, 1, 1, h, w)
                        # BLock 2 ------------------------------------------


                        # BLock 3 ------------------------------------------
                        f1_via_fa_num = smcorr_fa_num.sum((3, 4)).reshape(C, H * W)
                        # BLock 3 ------------------------------------------

                        # BLock 4 ------------------------------------------
                        corr2_num = torch.matmul(f1_via_fa_num.t(), f2_norm_num)
                        corr2_num = corr2_num.reshape(corr_num.shape)
                        smcorr2_num = F.softmax(corr2_num.reshape(H, W, -1), dim=2)
                        smcorr2_num = smcorr2_num.reshape(corr_num.shape)
                        # BLock 4 ------------------------------------------

                        grad_f1_num, grad_fa_num, grad_f2_num = torch.autograd.grad(
                            outputs=(smcorr2_num,),
                            inputs=(f1_num, fa_num, f2_num),
                            grad_outputs=(grad_smcorr2.view(corr_num.shape)),
                        )

                        rel_diff(grad_f1, grad_f1_num, "df1_")
                        rel_diff(grad_f2, grad_f2_num, "df2_")
                        rel_diff(grad_fa, grad_fa_num, "dfa_")

                """Distribute the gradients back among the input tensor
                features that require them."""
                grad_feats1[b] += grad_f1.reshape((C, H, W))
                grad_feats1[(b + 1) % B] += grad_fa.reshape((C, h, w))
                grad_feats2[b] += grad_f2.reshape((C, h, w))

                if PROFILE:
                    timings["feat-assign"] += time.time() - tic


            if LOCAL_CHECKS_INNER_LOOP:
                with torch.enable_grad():
                    loss = 0.
                    grad_loss_ = grad_loss * (H * W * B)  # unscale
                    for b in range(B):
                        f1 = feats1[b].reshape(C, H * W)  # source
                        f2 = feats2[b].reshape(C, h * w)  # target
                        fa = feats1[(b + 1) % B].reshape(C, h * w)  # auxiliary

                        if norm:
                            f1 = F.normalize(f1, p=2, dim=0) * JDT_FACTOR
                            f2 = F.normalize(f2, p=2, dim=0) * JDT_FACTOR
                            fa = F.normalize(fa, p=2, dim=0) * JDT_FACTOR

                        corr = torch.matmul(f1.t(), fa)
                        corr = corr.reshape(H, W, h, w)
                        smcorr = F.softmax(corr.reshape(H, W, -1), dim=2).reshape(corr.shape)
                        smcorr_fa = smcorr[None, ...] * fa.reshape(-1, 1, 1, h, w)
                        # del smcorr

                        f1_via_fa = smcorr_fa.sum((3, 4)).reshape(C, H * w)
                        # del smcorr_fa

                        corr2 = torch.matmul(f1_via_fa.t(), f2).reshape(corr.shape)
                        smcorr2 = F.softmax(corr2.reshape(H, W, -1), dim=2).reshape(corr.shape)
                        # del corr2

                        with torch.no_grad():
                            diff = batch_grid_u[b, :, :, None, None, :] - \
                                xxyy[None, None, ::stride, ::stride, :]
                            diff = (diff * diff).sum(4).sqrt()
                            diff = diff.pow(pow)
                        L = diff * smcorr2
                        loss += L.float().sum()

                    loss = loss / (H * W * B)
                    grad_f1_num, grad_f2_num = torch.autograd.grad(
                        outputs=loss,
                        inputs=(feats1, feats2),
                        grad_outputs=grad_loss_,
                    )

                    rel_diff(grad_feats1, grad_f1_num, "full-loop f2")
                    rel_diff(grad_feats2, grad_f2_num, "full-loop f2")

            if PROFILE:
                tic = time.time()

            if PRINT_MEM:
                key = None
                val = None
                shape_mems = {}
                for key, val in locals().items():
                    if hasattr(val, "shape"):
                        shape_mems[key] = estimate_mem(val)

                sorted_mems = sorted(shape_mems.items(), key=lambda kv: -kv[1])
                for key, val in sorted_mems:
                    print("{}: {:.4f} GiB".format(key, val))

            if PROFILE:
                timings["cleanup"] += time.time() - tic

            if PROFILE:
                timings["minibatch"] = time.time() - batch_tic
                print("==============")
                total_ratios = 0
                for key in timings:
                    ratio = 100 * timings[key] / timings["minibatch"]
                    msg = "{:.3f} ({:.2f}%) >>> {}"
                    print(msg.format(timings[key], ratio, key))
                    total_ratios += ratio
                msg = "{:.3f}s >>> ratio total {}"
                print(msg.format(timings["minibatch"], total_ratios - 100))
                print("==============")

        return (grad_feats1, grad_feats2, grad_xxyy, grad_batch_u,
                grad_stride, grad_norm, grad_pow)


def rel_diff(x1, x2, name):
    out = torch.abs(x1 - x2).sum() / torch.abs(x2).mean().clamp(EPS)
    print("rel diff for {}: {}".format(name, out))


def dense_corr_check(use_dve=False):
    # gradcheck takes a tuple of tensors as input, check if your gradient
    # evaluated with these tensors are close enough to numerical
    # approximations and returns True if they all verify this condition.
    dense_corr = DenseCorrDve.apply
    dve_dim = 4
    stride = 1
    norm = False
    B, C, H, W = 4, dve_dim, 4, 4

    common = {"dtype": torch.double, "requires_grad": True}
    if not norm:
        sc = 100
    else:
        sc = 1
    feats1 = torch.randn(B, C, H, W, **common) * sc
    feats2 = torch.randn(B, C, H, W, **common) * sc

    batch_grid_u = torch.randn(B, H, W, 2,
        dtype=torch.double,
        requires_grad=False,
    )

    H_input = H * stride
    W_input = W * stride
    xxyy = tps.spatial_grid_unnormalized(H_input, W_input).double()
    xxyy.requires_grad = False
    args = (feats1, feats2, xxyy, batch_grid_u, stride, norm)
    feats2.cuda()
    xxyy.cuda()
    batch_grid_u.cuda()
    test = gradcheck(dense_corr, args, eps=1e-6, atol=ATOL,
                     raise_exception=True)
    print("passed test: {}".format(test))


if __name__ == "__main__":
    dense_corr_check()
