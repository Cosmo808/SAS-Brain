import pdb
from tqdm import tqdm
import torch
from torch.nn import CrossEntropyLoss, BCEWithLogitsLoss, MSELoss
from torch.profiler import profile, record_function, ProfilerActivity
from timeit import default_timer as timer
import numpy as np
import copy
import os
from sklearn.metrics import balanced_accuracy_score, f1_score, confusion_matrix, cohen_kappa_score, roc_auc_score, \
    precision_recall_curve, auc, r2_score, mean_squared_error
from models.losses import *
from einops import rearrange
from spikingjelly.activation_based import functional
from scipy.interpolate import splrep, splev
from scipy.signal import resample_poly


class Trainer(object):
    def __init__(self, data_loaders, ann, snn, optimizer, scheduler, args):
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'

        self.snn = snn.to(self.device)
        self.ann = ann.to(self.device)
        self.optimizer = optimizer
        self.scheduler = scheduler
        self.args = args

        self.data_loaders = data_loaders
        self.criterion_ann = MSELoss().cuda()
        self.criterion_snn = MembraneLoss()

        self.iter = -1
        self.epoch = -1
        self.best_epoch = 0
        self.best_state_ann = None
        self.best_state_snn = None
        self.save_dir_snn = None
        self.save_dir_ann = None

        self.n_frames = None
        self.downstream_metric = None
        self.expect_spike_idxes = None
        self.spike_idxes = None
        self.MCMC_init(mode='max')

        if args.ckpt_snn is not None:
            self.best_state_snn = torch.load(args.ckpt_snn, map_location=self.device)
            self.snn.load_state_dict(self.best_state_snn)
            print(f"Loading snn ckpt from {args.ckpt_snn}")
        if args.ckpt_ann is not None:
            self.best_state_ann = torch.load(args.ckpt_ann, map_location=self.device)
            self.ann.load_state_dict(self.best_state_ann, strict=False)
            print(f"Loading ann ckpt from {args.ckpt_ann}")

    def ann_one_batch(self, x, y, events, training):
        with torch.no_grad():
            x_sas, y = self.snn_one_batch(x, y, events, slice=True)
        pred = self.ann(x_sas.to(self.device))

        if training:
            loss = self.criterion_ann(pred.flatten(), y.flatten())
            self.optimizer.zero_grad()
            loss.backward()
            if self.args.grad_clip > 0:
                torch.nn.utils.clip_grad_norm_(self.ann.parameters(), self.args.grad_clip)
            self.optimizer.step()
            assert self.scheduler[0].name == 'ann'
            self.scheduler[0].step()

            r_per_sample = torch.tensor([r2_score(pred[i].flatten().detach().cpu(), y[i].cpu()) for i in range(pred.shape[0])])
            if self.epoch > 0:
                self.MCMC_step(r_per_sample, mode='max')
            self.downstream_metric[self.iter] = r_per_sample

            return loss.detach().cpu().item()

        else:
            truth = y.flatten().cpu().squeeze().numpy().tolist()
            pred = pred.flatten().cpu().squeeze().numpy().tolist()
            return truth, pred

    def snn_one_batch(self, x, y, events, training=False, slice=False):
        B, L, C, T = x.shape

        if slice:
            x = rearrange(x, 'B L C T -> B C (L T)', L=L, T=T)
            x_sas = []
            for b in range(B):
                spike_idx = self.spike_idxes[self.iter, b]   # [L, 3]
                # spike_idx = [self.n_frames if len(s) == 0 else s[0].item() + 1 for s in spike_idx]
                flat_list = [s.item() + 1 + i * self.n_frames for i, row in enumerate(spike_idx) for s in row if s != -1]
                sorted_list = sorted(flat_list)
                spike_idx = [0] + sorted_list if sorted_list[0] != 0 else sorted_list
                spike_time = torch.tensor(spike_idx) / self.args.fps * self.args.sr
                spike_time = spike_time.to(torch.int64)
                x_sas.append(torch.stack(
                    [self.resample(x[b, :, spike_time[i]:spike_time[i + 1]], sample_num=T) for i in range(len(spike_time) - 1)]
                ))
            x_sas = torch.stack(x_sas).float().to(self.device)  # [B, l, C, T]
            if y.shape[1] != x_sas.shape[1]:
                y = F.interpolate(y.unsqueeze(1).float(), size=x_sas.shape[1], mode='linear').squeeze(1)
            return x_sas, y

        events = rearrange(events, 'B L t P C -> (B L) t P C', B=B, L=L)
        spike_idxes = self.snn(events.to(self.device))
        expect_idxes = self.expect_spike_idxes[self.iter].to(torch.int64)
        expect_idxes = rearrange(expect_idxes, 'B L k -> (B L) k', L=L)
        spike_loss = []
        for b in range(B * L):
            spike_idx = spike_idxes[b]
            expect_idx = expect_idxes[b]
            expect_idx = expect_idx[expect_idx != -1]

            no_spike = True if spike_idx.numel() == 0 else False
            expect_idx = expect_idx.unsqueeze(0) if expect_idx.ndim == 0 else expect_idx
            if spike_idx.numel() > self.expect_spike_idxes.shape[-1]:
                spike_idx = spike_idx[:self.expect_spike_idxes.shape[-1]]
            else:
                padded_idxes = torch.full((self.expect_spike_idxes.shape[-1],), - 1, dtype=spike_idx.dtype)
                padded_idxes[:spike_idx.size(0)] = spike_idx
                spike_idx = padded_idxes

            for i, exp in enumerate(expect_idx):
                s = spike_idx[i] if spike_idx[i] != -1 else self.n_frames
                mem_loss, I_loss = self.criterion_snn(self.snn.snn.node.past_v, self.snn.snn.I, b, s, exp, self.snn.snn.node.v_threshold, no_spike)
                spike_loss.append(mem_loss)

            if no_spike:
                spike_idx = torch.full((self.expect_spike_idxes.shape[-1],), - 1, dtype=spike_idx.dtype)
                spike_idx[0] = torch.sort(torch.randperm(self.n_frames)[0])[0] if not self.args.frozen_snn else self.n_frames - 1
            self.spike_idxes[self.iter, b // L, b % L] = spike_idx.cpu()

        spike_loss = sum(spike_loss) / len(spike_loss)

        if training:
            self.optimizer.zero_grad()
            spike_loss.backward()
            if self.args.grad_clip > 0:
                torch.nn.utils.clip_grad_norm_(self.snn.parameters(), self.args.grad_clip)
            self.optimizer.step()
            assert self.scheduler[1].name == 'snn'
            self.scheduler[1].step()

        functional.reset_net(self.snn.snn)
        self.snn.snn.I = []
        return spike_loss.detach().cpu().item()

    def run_one_epoch(self, mode):
        self.iter = -1
        if mode == 'train':
            self.ann.train()
            self.snn.train()
            spike_losses = []
            losses = []
            for x, y, events, subjects in tqdm(self.data_loaders[mode]):
                self.iter += 1
                y = y.to(self.device)

                if self.args.frozen_snn:
                    spike_losses.append(0)
                else:
                    spike_loss = self.snn_one_batch(x, y, events, training=True)
                    spike_losses.append(spike_loss)

                if self.args.frozen_ann:
                    losses.append(0)
                else:
                    loss = self.ann_one_batch(x, y, events, training=True)
                    losses.append(loss)

            return losses, spike_losses
        else:
            self.ann.eval()
            truths = []
            preds = []
            for x, y, events, subjects in tqdm(self.data_loaders[mode]):
                self.iter += 1
                y = y.to(self.device)

                spike_loss = self.snn_one_batch(x, y, events, training=False)
                truth, pred = self.ann_one_batch(x, y, events, training=False)
                truths += truth
                preds += pred

            truths = np.array(truths)
            preds = np.array(preds)
            corrcoef = np.corrcoef(truths, preds)[0, 1]
            r2 = r2_score(truths, preds)
            rmse = mean_squared_error(truths, preds) ** 0.5
            print("Truth mean/std:", truths.mean(), truths.std())
            print("Pred mean/std:", preds.mean(), preds.std())
            print("MSE:", self.criterion_ann(torch.tensor(preds, device=self.device), torch.tensor(truths, device=self.device)).item())
            return corrcoef, r2, rmse, spike_loss

    def train(self):
        start_time = timer()
        corrcoef_best = 0
        r2_best = -np.inf
        rmse_best = 0
        spike_best = np.inf
        for epoch in range(self.args.max_epoch):
            self.epoch = epoch
            losses, spike_losses = self.run_one_epoch(mode='train')
            optim_state = self.optimizer.state_dict()

            with torch.no_grad():
                corrcoef, r2, rmse, spike_loss = self.run_one_epoch(mode='test')

                print(
                    "Epoch {}/{} | training loss: {:.5f}/{:.3f}, cor: {:.5f}, r2: {:.5f}, rmse: {:.5f}, LR: {:.2e}, elapsed {:.1f} mins".format(
                        epoch, self.args.max_epoch, np.mean(losses), np.mean(spike_losses), corrcoef, r2, rmse,
                        optim_state['param_groups'][0]['lr'], (timer() - start_time) / 60)
                )
                if r2 > r2_best:
                    print("val metric increasing....saving weights !! ")
                    print(
                        "Val Evaluation: cor: {:.5f}, r2: {:.5f}, rmse: {:.5f}, spike_loss: {:.3f}".format(
                            corrcoef, r2, rmse, spike_loss)
                    )
                    self.best_epoch = epoch
                    corrcoef_best = corrcoef
                    r2_best = r2
                    rmse_best = rmse
                    spike_best = spike_loss
                    self.best_state_ann = copy.deepcopy(self.ann.state_dict())
                    self.best_state_snn = copy.deepcopy(self.snn.state_dict())
                    self.save_dict((corrcoef, r2, rmse, spike_loss))
                print(f"Epoch {epoch}/{self.args.max_epoch} fnished...\n\n")

        self.ann.load_state_dict(self.best_state_ann, strict=False)
        self.snn.load_state_dict(self.best_state_snn)
        with torch.no_grad():
            print("***************************Test results************************")
            corrcoef, r2, rmse, spike_loss = self.run_one_epoch(mode='test')
            print(
                "Test Evaluation: cor: {:.5f}, r2: {:.5f}, rmse: {:.5f}, spike_loss: {:.5f}".format(
                    corrcoef, r2, rmse, spike_loss)
            )
            self.epoch += 1
            self.save_dict((corrcoef, r2, rmse, spike_loss))

    def save_dict(self, values):
        if self.epoch == 0:
            return

        if self.save_dir_ann is not None:
            if os.path.exists(self.save_dir_ann):
                os.remove(self.save_dir_ann)
        if self.save_dir_snn is not None:
            if os.path.exists(self.save_dir_snn):
                os.remove(self.save_dir_snn)

        corrcoef, r2, rmse, spike_loss = values
        self.save_dir_ann = self.args.save_dir + r"\ann_epoch{}_cor_{:.5f}_r2_{:.5f}_rmse_{:.5f}.pth".format(self.best_epoch, corrcoef, r2, rmse)
        self.save_dir_snn = self.args.save_dir + r"\snn_epoch{}_spike_{:.5f}.pth".format(self.best_epoch, spike_loss)
        os.makedirs(os.path.dirname(self.save_dir_ann), exist_ok=True)
        os.makedirs(os.path.dirname(self.save_dir_snn), exist_ok=True)

        torch.save(self.best_state_ann, f=self.save_dir_ann)
        torch.save(self.best_state_snn, f=self.save_dir_snn)
        print("ann model save in " + self.save_dir_ann)
        print("snn model save in " + self.save_dir_snn)

    def MCMC_init(self, mode):
        for x, y, events, subjects in self.data_loaders['train']:
            B, L, C, T = x.shape  # B, 5, 17, 1600
            break
        duration = T / self.args.sr  # 8 seconds
        self.n_frames = int(duration * self.args.fps)  # 24 frames
        # expect_spike_idxes = torch.ones(size=[len(self.data_loaders['train']), self.args.bs, L]) * (self.n_frames - 1)
        self.expect_spike_idxes = torch.stack([
            torch.sort(torch.randperm(self.n_frames)[:self.args.n_slice])[0]
            for _ in range(len(self.data_loaders['train']) * self.args.bs * L)
        ]).view(len(self.data_loaders['train']), self.args.bs, L, self.args.n_slice)
        self.spike_idxes = torch.zeros_like(self.expect_spike_idxes) - 1
        self.spike_idxes[:, :, :, 0] = self.n_frames - 1
        self.downstream_metric = torch.zeros(size=[len(self.data_loaders['train']), self.args.bs], device=self.device)
        if mode == 'min':
            self.downstream_metric += np.inf

    def MCMC_step(self, metric, mode):    # MCMC
        accept = []
        for b in range(self.args.bs):
            u = torch.rand(1).item()
            if mode == 'min':
                p = self.downstream_metric[self.iter, b] / metric[b]
            elif mode == 'max':
                p = metric[b] / self.downstream_metric[self.iter, b]

            if u < p:  # accept
                accept.append(b)
                self.expect_spike_idxes[self.iter, b] = self.spike_idxes[self.iter, b]
        # print(f"Accept {len(accept)}/{self.args.bs} batches for new slicing labels for iter {self.iter} ")

    def resample(self, rep, sample_num):
        assert rep.dim() == 2
        if rep.shape[-1] == sample_num:
            return rep

        if rep.shape[-1] < sample_num:  # upsample
            features, time = rep.shape
            x_original = np.linspace(0, 1, time)
            x_target = np.linspace(0, 1, sample_num)
            interpolated_rep = np.zeros((features, sample_num))
            for j in range(features):
                tck = splrep(x_original, rep[j, :].cpu().numpy(), k=3, s=0)
                interpolated_rep[j, :] = splev(x_target, tck)
        else:  # downsample
            interpolated_rep = resample_poly(rep, up=sample_num, down=rep.shape[-1], axis=1)

        interpolated_rep = torch.tensor(interpolated_rep)
        # if interpolated_rep.dim() == 2:
        #     interpolated_rep = interpolated_rep.unsqueeze(0)
        return interpolated_rep