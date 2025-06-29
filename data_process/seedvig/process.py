import h5py
import scipy
from tqdm import tqdm
import os
from models.utils import Brain2Event
import torch

data_dir = r'E:\NIPS2026\datasets\SEED-VIG\Raw_Data'
labels_dir = r'E:\NIPS2026\datasets\SEED-VIG\perclos_labels'

files = [file for file in os.listdir(data_dir)]
files = sorted(files)

files_dict = {
    'train': files[:15],
    'val': files[15:19],
    'test': files[19:23],
}

print(files_dict)

dataset = {
    'train': list(),
    'val': list(),
    'test': list(),
}

class Param:
    pass
param = Param()
param.C = 0.2
param.fps = 3
param.sr = 200
b2e = Brain2Event(param)

for files_key in files_dict.keys():
    seq_dir = rf'E:\NIPS2026\datasets\SEED-VIG\{files_key}\seq'
    label_dir = rf'E:\NIPS2026\datasets\SEED-VIG\{files_key}\labels'
    event_dir = rf'E:\NIPS2026\datasets\SEED-VIG\{files_key}\events'
    for file in tqdm(files_dict[files_key]):
        eegs = scipy.io.loadmat(os.path.join(data_dir, file))['EEG'][0][0][0]
        labels = scipy.io.loadmat(os.path.join(labels_dir, file))['perclos']

        eegs = eegs.reshape(885, 8 * 200, 17)
        eegs = eegs.transpose(0, 2, 1)
        labels = labels[:, 0]
        eegs = torch.tensor(eegs).float().view(177, 5, 17, 1600)
        labels = torch.tensor(labels).view(177, 5)

        epochs_events = []
        for seq in eegs:
            events = b2e.forward(seq)
            epochs_events.append(events)
        epochs_events = torch.stack(epochs_events)
        
        subject_id = file.split('.')[0]
        os.makedirs(rf"{seq_dir}\{subject_id}", exist_ok=True)
        os.makedirs(rf"{label_dir}\{subject_id}", exist_ok=True)
        os.makedirs(rf"{event_dir}\{subject_id}", exist_ok=True)
        num = 0
        for eeg, label, event in zip(eegs, labels, epochs_events):
            torch.save(eeg.clone(), rf"{seq_dir}\{subject_id}\{num}.pth")   # [5, 17, 1600]
            torch.save(label.clone(), rf"{label_dir}\{subject_id}\{num}.pth")
            torch.save(event.clone(), rf"{event_dir}\{subject_id}\{num}.pth")   # [5, 24, 2, 17]
            num += 1

