import os
import glob
import torch
import numpy as np
import pandas as pd
import torch.nn as nn
from tqdm import tqdm
import engine
import config
from torch.utils.tensorboard import SummaryWriter
from utils.dataset import DataLoaderRhythmNet
from utils.plot_scripts import plot_train_test_curves, bland_altman_plot, gt_vs_est, create_plot_for_tensorboard
from utils.model_utils import plot_loss, load_model_if_checkpointed, save_model_checkpoint
from models.simpleCNN import SimpleCNN
from models.lenet import LeNet
from models.rhythmNet import RhythmNet
from loss_fn.rhythmnet_loss import RhythmNetLoss


def run_training():

    # check path to checkpoint directory
    if config.CHECKPOINT_PATH:
        if not os.path.exists(config.CHECKPOINT_PATH):
            os.makedirs(config.CHECKPOINT_PATH)
            print("Output directory is created")

    # --------------------------------------
    # Initialize Model
    # --------------------------------------

    model = RhythmNet()

    if torch.cuda.is_available():
        print('GPU available... using GPU')
        torch.cuda.manual_seed_all(42)
    else:
        print("GPU not available, using CPU")

    if config.CHECKPOINT_PATH:
        checkpoint_path = os.path.join(os.getcwd(), config.CHECKPOINT_PATH)
        if not os.path.exists(checkpoint_path):
            os.makedirs(checkpoint_path)
            print("Output directory is created")

    # device = torch.device('cuda:0') if torch.cuda.is_available() else torch.device('cpu')

    model.to(config.DEVICE)

    optimizer = torch.optim.Adam(model.parameters(), lr=config.lr)
    # scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
    #     optimizer, factor=0.8, patience=5, verbose=True
    # )
    # loss_fn = nn.L1Loss()
    loss_fn = RhythmNetLoss()

    testset = trainset = None
    st_maps = glob.glob(config.ST_MAPS_PATH + '*.npy')

    # Initialize SummaryWriter object
    writer = SummaryWriter()

    # Read from a pre-made csv file that contains data divided into folds for cross validation
    folds_df = pd.read_csv(config.SAVE_CSV_PATH)

    # Loop for enumerating through folds.
    print(f"Details: {len(folds_df['iteration'].unique())} fold training for {config.EPOCHS} Epochs (each video)")
    # for k in folds_df['iteration'].unique():
    for k in [1]:
        # Filter DF
        video_files_test = folds_df.loc[(folds_df['iteration'] == k) & (folds_df['set'] == 'V')]
        video_files_train = folds_df.loc[(folds_df['iteration'] == k) & (folds_df['set'] == 'T')]

        # Get paths from filtered DF
        video_files_test = [os.path.join(config.ST_MAPS_PATH, video_path) for video_path in
                            video_files_test["video"].values]
        video_files_train = [os.path.join(config.ST_MAPS_PATH, video_path) for video_path in
                             video_files_train["video"].values]

        # print(f"Reading Current File: {video_file_path}")
        train_set = DataLoaderRhythmNet(st_maps_path=video_files_train, target_signal_path=config.TARGET_SIGNAL_DIR)

        # --------------------------------------
        # Build Dataloaders
        # --------------------------------------

        train_loader = torch.utils.data.DataLoader(
            dataset=train_set,
            batch_size=None,
            num_workers=config.NUM_WORKERS,
            shuffle=False
        )
        print('\nTrain DataLoader constructed successfully!')

        test_set = DataLoaderRhythmNet(st_maps_path=video_files_test, target_signal_path=config.TARGET_SIGNAL_DIR)
        test_loader = torch.utils.data.DataLoader(
            dataset=test_set,
            batch_size=None,
            num_workers=config.NUM_WORKERS,
            shuffle=False
        )
        print('\nEvaluation DataLoader constructed successfully!')

        # Code to use multiple GPUs (if available)
        if torch.cuda.device_count() > 1:
            print("Let's use", torch.cuda.device_count(), "GPUs!")
            model = torch.nn.DataParallel(model)

        # --------------------------------------
        # Load checkpointed model (if  present)
        # --------------------------------------
        if config.DEVICE == "cpu":
            load_on_cpu = True
        else:
            load_on_cpu = False
        model, optimizer, checkpointed_loss, checkpoint_flag = load_model_if_checkpointed(model, optimizer, checkpoint_path, load_on_cpu=load_on_cpu)
        if checkpoint_flag:
            print(f"Checkpoint Found! Loading from checkpoint :: LOSS={checkpointed_loss}")
        else:
            print("Checkpoint Not Found! Training from beginning")

        # -----------------------------
        # Start training
        # -----------------------------

        train_loss_per_epoch = []
        # train_loss = 0.0
        for epoch in range(config.EPOCHS):
            # short-circuit for evaluation
            if k == 1:
                break
            train_loss = engine.train_fn(model, train_loader, optimizer, loss_fn, save_model=True)

            print(f"\nFinished => [Epoch: {epoch + 1}/{config.EPOCHS} ",
                  "Training Loss: {:.3f} ".format(train_loss))

            # Save model with final train loss (script to save the best weights?)
            if checkpointed_loss != 0.0:
                if train_loss < checkpointed_loss:
                    save_model_checkpoint(model, optimizer, train_loss, checkpoint_path)
                    checkpointed_loss = train_loss
                else:
                    pass
            else:
                if len(train_loss_per_epoch) > 0:
                    if train_loss < min(train_loss_per_epoch):
                        save_model_checkpoint(model, optimizer, train_loss, checkpoint_path)
                else:
                    save_model_checkpoint(model, optimizer, train_loss, checkpoint_path)

            train_loss_per_epoch.append(train_loss)
            writer.add_scalar("Loss/train", train_loss, epoch)

        mean_loss = np.mean(train_loss_per_epoch)
        # Save the mean_loss value for each video instance to the writer
        print(f"Avg Training Loss: {np.mean(mean_loss)} for {config.EPOCHS} epochs")
        writer.flush()

        # -----------------------------
        # Start Validation
        # -----------------------------

        print(f"Validating {len(video_files_test)} video files for {config.EPOCHS_TEST} Epochs")

        # # --------------------------------------
        # # Load checkpointed model (if  present)
        # # --------------------------------------
        # if config.DEVICE == "cpu":
        #     load_on_cpu = True
        # else:
        #     load_on_cpu = False
        # model, optimizer, loss, checkpoint_flag = load_model_if_checkpointed(model, optimizer, checkpoint_path, load_on_cpu=load_on_cpu)

        eval_loss_per_epoch = []
        for epoch in range(config.EPOCHS_TEST):
            # validation
            target_hr_list, predicted_hr_list, eval_loss = engine.eval_fn(model, test_loader, loss_fn)

            # truth_hr_list.append(target)
            # estimated_hr_list.append(predicted)
            print(f"Epoch {epoch} => Val Loss: {eval_loss}")
            # writer.add_scalars('gt_vs_est_hr', {'true_hr': target, 'estimated_hr': predicted}, idx)
            eval_loss_per_epoch.append(eval_loss)
            writer.add_scalar("Loss/test", mean_loss, epoch)

            # Plots on tensorboard
            ba_plot_image = create_plot_for_tensorboard('bland_altman', target_hr_list, predicted_hr_list)
            gtvsest_plot_image = create_plot_for_tensorboard('gt_vs_est', target_hr_list, predicted_hr_list)
            writer.add_image('BA_plot', ba_plot_image, epoch)
            writer.add_image('gtvsest_plot', gtvsest_plot_image, epoch)

        mean_test_loss = np.mean(eval_loss_per_epoch)

        print(f"Avg Validation Loss: {mean_test_loss} for {config.EPOCHS_TEST} epochs")
        writer.flush()
        # plot_train_test_curves(train_loss_data, test_loss_data, plot_path=config.PLOT_PATH, fold_tag=k)
        # Plots on the local storage.
        gt_vs_est(target_hr_list, predicted_hr_list, plot_path=config.PLOT_PATH)
        bland_altman_plot(target_hr_list, predicted_hr_list, plot_path=config.PLOT_PATH)
        writer.close()
        print("done")


if __name__ == '__main__':
    run_training()
