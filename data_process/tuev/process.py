import mne
import numpy as np
import os
import pickle
from tqdm import tqdm
import shutil

"""
https://github.com/Abhishaike/EEG_Event_Classification
"""


def BuildEvents(signals, times, EventData):
    [numEvents, z] = EventData.shape  # numEvents is equal to # of rows of the .rec file
    fs = 200.0
    [numChan, numPoints] = signals.shape
    # for i in range(numChan):  # standardize each channel
    #     if np.std(signals[i, :]) > 0:
    #         signals[i, :] = (signals[i, :] - np.mean(signals[i, :])) / np.std(signals[i, :])
    features = np.zeros([numEvents, numChan, int(fs) * 5])
    offending_channel = np.zeros([numEvents, 1])  # channel that had the detected thing
    labels = np.zeros([numEvents, 1])
    offset = signals.shape[1]
    signals = np.concatenate([signals, signals, signals], axis=1)
    for i in range(numEvents):  # for each event
        chan = int(EventData[i, 0])  # chan is channel
        start = np.where((times) >= EventData[i, 1])[0][0]
        end = np.where((times) >= EventData[i, 2])[0][0]
        # print (offset + start - 2 * int(fs), offset + end + 2 * int(fs), signals.shape)
        features[i, :] = signals[
            :, offset + start - 2 * int(fs) : offset + end + 2 * int(fs)
        ]
        offending_channel[i, :] = int(chan)
        labels[i, :] = int(EventData[i, 3])
    return [features, offending_channel, labels]


def convert_signals(signals, Rawdata):
    signal_names = {
        k: v
        for (k, v) in zip(
            Rawdata.info["ch_names"], list(range(len(Rawdata.info["ch_names"])))
        )
    }
    new_signals = np.vstack(
        (
            signals[signal_names["EEG FP1-REF"]]
            - signals[signal_names["EEG F7-REF"]],  # 0
            (
                signals[signal_names["EEG F7-REF"]]
                - signals[signal_names["EEG T3-REF"]]
            ),  # 1
            (
                signals[signal_names["EEG T3-REF"]]
                - signals[signal_names["EEG T5-REF"]]
            ),  # 2
            (
                signals[signal_names["EEG T5-REF"]]
                - signals[signal_names["EEG O1-REF"]]
            ),  # 3
            (
                signals[signal_names["EEG FP2-REF"]]
                - signals[signal_names["EEG F8-REF"]]
            ),  # 4
            (
                signals[signal_names["EEG F8-REF"]]
                - signals[signal_names["EEG T4-REF"]]
            ),  # 5
            (
                signals[signal_names["EEG T4-REF"]]
                - signals[signal_names["EEG T6-REF"]]
            ),  # 6
            (
                signals[signal_names["EEG T6-REF"]]
                - signals[signal_names["EEG O2-REF"]]
            ),  # 7
            (
                signals[signal_names["EEG FP1-REF"]]
                - signals[signal_names["EEG F3-REF"]]
            ),  # 14
            (
                signals[signal_names["EEG F3-REF"]]
                - signals[signal_names["EEG C3-REF"]]
            ),  # 15
            (
                signals[signal_names["EEG C3-REF"]]
                - signals[signal_names["EEG P3-REF"]]
            ),  # 16
            (
                signals[signal_names["EEG P3-REF"]]
                - signals[signal_names["EEG O1-REF"]]
            ),  # 17
            (
                signals[signal_names["EEG FP2-REF"]]
                - signals[signal_names["EEG F4-REF"]]
            ),  # 18
            (
                signals[signal_names["EEG F4-REF"]]
                - signals[signal_names["EEG C4-REF"]]
            ),  # 19
            (
                signals[signal_names["EEG C4-REF"]]
                - signals[signal_names["EEG P4-REF"]]
            ),  # 20
            (signals[signal_names["EEG P4-REF"]] - signals[signal_names["EEG O2-REF"]]),
        )
    )  # 21
    return new_signals


def readEDF(fileName):
    Rawdata = mne.io.read_raw_edf(fileName, preload=True)
    Rawdata.resample(200)
    Rawdata.filter(l_freq=0.3, h_freq=75)
    Rawdata.notch_filter((60))

    _, times = Rawdata[:]
    signals = Rawdata.get_data(units='uV')
    RecFile = fileName[0:-3] + "rec"
    eventData = np.genfromtxt(RecFile, delimiter=",")
    Rawdata.close()
    return [signals, times, eventData, Rawdata]


def load_up_objects(BaseDir, Features, OffendingChannels, Labels, OutDir):
    for dirName, subdirList, fileList in tqdm(os.walk(BaseDir)):
        print("Found directory: %s" % dirName)
        for fname in fileList:
            if fname[-4:] == ".edf":
                print("\t%s" % fname)
                try:
                    [signals, times, event, Rawdata] = readEDF(
                        dirName + "/" + fname
                    )  # event is the .rec file in the form of an array
                    signals = convert_signals(signals, Rawdata)
                except (ValueError, KeyError):
                    print("something funky happened in " + dirName + "/" + fname)
                    continue
                signals, offending_channels, labels = BuildEvents(signals, times, event)

                for idx, (signal, offending_channel, label) in enumerate(
                    zip(signals, offending_channels, labels)
                ):
                    sample = {
                        "signal": signal,
                        "offending_channel": offending_channel,
                        "label": label,
                    }
                    save_pickle(
                        sample,
                        os.path.join(
                            OutDir, fname.split(".")[0] + "-" + str(idx) + ".pkl"
                        ),
                    )

    return Features, Labels, OffendingChannels


def save_pickle(object, filename):
    with open(filename, "wb") as f:
        pickle.dump(object, f)


"""
TUEV dataset is downloaded from https://isip.piconepress.com/projects/tuh_eeg/html/downloads.shtml
"""

# root = "/data/zcb/data/TUEV/edf"
root = r"E:\NIPS2026\datasets\TUEV\edf"
# target = "/data/datasets/BigDownstream/TUEV_cbramod"
target = r"E:\NIPS2026\datasets\TUEV\edf"
train_out_dir = os.path.join(target, "processed_train")
eval_out_dir = os.path.join(target, "processed_eval")
if not os.path.exists(train_out_dir):
    os.makedirs(train_out_dir)
if not os.path.exists(eval_out_dir):
    os.makedirs(eval_out_dir)

BaseDirTrain = os.path.join(root, "train")
fs = 200
TrainFeatures = np.empty(
    (0, 16, fs)
)  # 0 for lack of intialization, 22 for channels, fs for num of points
TrainLabels = np.empty([0, 1])
TrainOffendingChannel = np.empty([0, 1])
# load_up_objects(
#     BaseDirTrain, TrainFeatures, TrainLabels, TrainOffendingChannel, train_out_dir
# )

BaseDirEval = os.path.join(root, "eval")
fs = 200
EvalFeatures = np.empty(
    (0, 16, fs)
)  # 0 for lack of intialization, 22 for channels, fs for num of points
EvalLabels = np.empty([0, 1])
EvalOffendingChannel = np.empty([0, 1])
# load_up_objects(
#     BaseDirEval, EvalFeatures, EvalLabels, EvalOffendingChannel, eval_out_dir
# )


#transfer to train, eval, and test
# root = "/data/datasets/BigDownstream/TUEV_cbramod"
root = r"E:\NIPS2026\datasets\TUEV\edf"
seed = 4523
np.random.seed(seed)

train_files = os.listdir(os.path.join(root, "processed_train"))
train_sub = list(set([f.split("_")[0] for f in train_files]))
print("train sub", len(train_sub))
test_files = os.listdir(os.path.join(root, "processed_eval"))

val_sub = np.random.choice(train_sub, size=int(
    len(train_sub) * 0.2), replace=False)
train_sub = list(set(train_sub) - set(val_sub))
val_files = [f for f in train_files if f.split("_")[0] in val_sub]
train_files = [f for f in train_files if f.split("_")[0] in train_sub]


if not os.path.exists(os.path.join(root, 'processed_eeg', 'train')):
    os.makedirs(os.path.join(root, 'processed_eeg', 'train'))
if not os.path.exists(os.path.join(root, 'processed_eeg', 'val')):
    os.makedirs(os.path.join(root, 'processed_eeg', 'val'))
if not os.path.exists(os.path.join(root, 'processed_eeg', 'test')):
    os.makedirs(os.path.join(root, 'processed_eeg', 'test'))

for file in tqdm(train_files):
    shutil.copy(
        os.path.join(root, 'processed_train', file),
        os.path.join(root, 'processed_eeg', 'train', file)
    )
for file in tqdm(val_files):
    shutil.copy(
        os.path.join(root, 'processed_train', file),
        os.path.join(root, 'processed_eeg', 'val', file)
    )
for file in tqdm(test_files):
    shutil.copy(
        os.path.join(root, 'processed_eval', file),
        os.path.join(root, 'processed_eeg', 'test', file)
    )

print('Done!')
