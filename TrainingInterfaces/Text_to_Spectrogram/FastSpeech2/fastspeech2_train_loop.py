import os
import time

import librosa.display as lbd
import matplotlib.pyplot as plt
import torch
import torch.multiprocessing
from torch.cuda.amp import GradScaler
from torch.cuda.amp import autocast
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data.dataloader import DataLoader
from tqdm import tqdm

from Preprocessing.TextFrontend import TextFrontend
from Utility.WarmupScheduler import WarmupScheduler
from Utility.utils import cumsum_durations
from Utility.utils import delete_old_checkpoints
from Utility.utils import get_most_recent_checkpoint


def plot_progress_spec(net, device, save_dir, step, lang, reference_speaker_embedding_for_plot):
    tf = TextFrontend(language=lang, use_word_boundaries=False, use_explicit_eos=False)
    sentence = "Hello"
    if lang == "en":
        sentence = "This is an unseen sentence."
    elif lang == "de":
        sentence = "Dies ist ein ungesehener Satz."
    phoneme_vector = tf.string_to_tensor(sentence).long().squeeze(0).to(device)
    spec, durations, *_ = net.inference(text=phoneme_vector, speaker_embeddings=reference_speaker_embedding_for_plot, return_duration_pitch_energy=True)
    spec = spec.transpose(0, 1).to("cpu").numpy()
    duration_splits, label_positions = cumsum_durations(durations.cpu().numpy())
    if not os.path.exists(os.path.join(save_dir, "spec")):
        os.makedirs(os.path.join(save_dir, "spec"))
    fig, ax = plt.subplots(nrows=1, ncols=1)
    lbd.specshow(spec,
                 ax=ax,
                 sr=16000,
                 cmap='GnBu',
                 y_axis='mel',
                 x_axis=None,
                 hop_length=256)
    ax.yaxis.set_visible(False)
    ax.set_xticks(duration_splits, minor=True)
    ax.xaxis.grid(True, which='minor')
    ax.set_xticks(label_positions, minor=False)
    ax.set_xticklabels(tf.get_phone_string(sentence)[:-1])
    ax.set_title(sentence)
    plt.savefig(os.path.join(os.path.join(save_dir, "spec"), str(step) + ".png"))
    plt.clf()
    plt.close()


def collate_and_pad(batch):
    if len(batch[0]) == 8:
        # text, text_len, speech, speech_len, durations, energy, pitch, speaker_emb
        return (pad_sequence([datapoint[0] for datapoint in batch], batch_first=True),
                torch.stack([datapoint[1] for datapoint in batch]).squeeze(1),
                pad_sequence([datapoint[2] for datapoint in batch], batch_first=True),
                torch.stack([datapoint[3] for datapoint in batch]).squeeze(1),
                pad_sequence([datapoint[4] for datapoint in batch], batch_first=True),
                pad_sequence([datapoint[5] for datapoint in batch], batch_first=True),
                pad_sequence([datapoint[6] for datapoint in batch], batch_first=True),
                torch.stack([datapoint[7] for datapoint in batch]))
    else:
        # text, text_len, speech, speech_len, durations, energy, pitch
        return (pad_sequence([datapoint[0] for datapoint in batch], batch_first=True),
                torch.stack([datapoint[1] for datapoint in batch]).squeeze(1),
                pad_sequence([datapoint[2] for datapoint in batch], batch_first=True),
                torch.stack([datapoint[3] for datapoint in batch]).squeeze(1),
                pad_sequence([datapoint[4] for datapoint in batch], batch_first=True),
                pad_sequence([datapoint[5] for datapoint in batch], batch_first=True),
                pad_sequence([datapoint[6] for datapoint in batch], batch_first=True))


def train_loop(net,
               train_dataset,
               device,
               save_directory,
               batch_size=32,
               steps=300000,
               epochs_per_save=5,
               use_speaker_embedding=False,
               lang="en",
               lr=0.001,
               warmup_steps=14000,
               path_to_checkpoint=None,
               fine_tune=False,
               resume=False):
    """
    Args:
        resume: whether to resume from the most recent checkpoint
        warmup_steps: how long the learning rate should increase before it reaches the specified value
        steps: How many steps to train
        lr: The initial learning rate for the optimiser
        path_to_checkpoint: reloads a checkpoint to continue training from there
        fine_tune: whether to load everything from a checkpoint, or only the model parameters
        lang: language of the synthesis
        use_speaker_embedding: whether to expect speaker embeddings
        net: Model to train
        train_dataset: Pytorch Dataset Object for train data
        device: Device to put the loaded tensors on
        save_directory: Where to save the checkpoints
        batch_size: How many elements should be loaded at once
        epochs_per_save: how many epochs to train in between checkpoints
    """
    net = net.to(device)
    scaler = GradScaler()
    if use_speaker_embedding:
        reference_speaker_embedding_for_plot = torch.Tensor(train_dataset[0][7]).to(device)
    else:
        reference_speaker_embedding_for_plot = None
    torch.multiprocessing.set_sharing_strategy('file_system')
    train_loader = DataLoader(batch_size=batch_size,
                              dataset=train_dataset,
                              drop_last=True,
                              num_workers=8,
                              pin_memory=False,
                              shuffle=True,
                              prefetch_factor=8,
                              collate_fn=collate_and_pad,
                              persistent_workers=True)
    step_counter = 0
    net.train()
    if fine_tune:
        lr = lr * 0.01
    optimizer = torch.optim.Adam(net.parameters(), lr=lr)
    scheduler = WarmupScheduler(optimizer, warmup_steps=warmup_steps)
    epoch = 0
    if resume:
        path_to_checkpoint = get_most_recent_checkpoint(checkpoint_dir=save_directory)
    if path_to_checkpoint is not None:
        # careful when restarting, plotting data will be overwritten!
        check_dict = torch.load(path_to_checkpoint, map_location=device)
        net.load_state_dict(check_dict["model"])
        if not fine_tune:
            optimizer.load_state_dict(check_dict["optimizer"])
            scaler.load_state_dict(check_dict["scaler"])
            scheduler.load_state_dict(check_dict["scheduler"])
            step_counter = check_dict["step_counter"]
    start_time = time.time()
    while True:
        epoch += 1
        optimizer.zero_grad()
        train_losses_this_epoch = list()
        for batch in tqdm(train_loader):
            with autocast():
                if not use_speaker_embedding:
                    train_loss = net(batch[0].to(device), batch[1].to(device), batch[2].to(device),
                                     batch[3].to(device), batch[4].to(device), batch[6].to(device),
                                     batch[5].to(device))
                else:
                    train_loss = net(batch[0].to(device), batch[1].to(device), batch[2].to(device),
                                     batch[3].to(device), batch[4].to(device), batch[6].to(device),
                                     batch[5].to(device), batch[7].to(device))
                train_losses_this_epoch.append(train_loss.item())
            optimizer.zero_grad()
            scaler.scale(train_loss).backward()
            del train_loss
            step_counter += 1
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(net.parameters(), 1.0, error_if_nonfinite=False)
            scaler.step(optimizer)
            scaler.update()
            scheduler.step()
        with torch.no_grad():
            net.eval()
            if epoch % epochs_per_save == 0:
                torch.save({
                    "model"       : net.state_dict(),
                    "optimizer"   : optimizer.state_dict(),
                    "scaler"      : scaler.state_dict(),
                    "step_counter": step_counter,
                    "scheduler"   : scheduler.state_dict(),
                    }, os.path.join(save_directory, "checkpoint_{}.pt".format(step_counter)))
                delete_old_checkpoints(save_directory, keep=5)
                plot_progress_spec(net, device, save_dir=save_directory, step=step_counter, lang=lang,
                                   reference_speaker_embedding_for_plot=reference_speaker_embedding_for_plot)
                if step_counter > steps:
                    # DONE
                    return
            print("Epoch:        {}".format(epoch))
            print("Train Loss:   {}".format(sum(train_losses_this_epoch) / len(train_losses_this_epoch)))
            print("Time elapsed: {} Minutes".format(round((time.time() - start_time) / 60)))
            print("Steps:        {}".format(step_counter))
            torch.cuda.empty_cache()
            net.train()
