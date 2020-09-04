import json
import sys
import torch
import cv2
import numpy as np
import torchvision.transforms as T
import random
from collections import defaultdict
from pathlib import Path
from PIL import Image
from torch import optim
from torch.utils.data import DataLoader
from tqdm import tqdm
from src.dataset.YT_Greenscreen import YT_Greenscreen
from src.utils import initiator, time_logger, AverageMeter, stack, eval_metrics, fast_hist
from src.utils.visualizations import visualize_logger

from src.utils.metrics import get_gpu_memory_map


class GridTrainer:
    def __init__(self, config, train=True, batch_size=None, load_from_checkpoint=True, num_workers=1):
        self.seed = 0

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.config = config
        self.model, self.weight_decay, self.lr_boundarys, self.detach_interval = initiator.initiate_model(self.config)
        self.model = self.model.to(self.device)
        self.criterion = initiator.initiate_criterion(self.config)
        # self.logger = initiator.initiate_logger(self.lr_boundarys[0])
        self.logger = defaultdict(list)
        self.metric_logger = defaultdict(list)

        self.batch_size = self.config["batch_size"] if batch_size is None else batch_size
        self.time_logger = time_logger.TimeLogger(restart_time=60 * 60 * 1.19)  # 60 * 60 * 1.2
        self.dataset = YT_Greenscreen(train=train, start_index=0,
                                      batch_size=self.batch_size, seed=self.seed)
        self.loader = DataLoader(dataset=self.dataset, shuffle=False,
                                 batch_size=self.batch_size)
        self.optimizer = optim.Adam(self.model.parameters(),
                                    lr=self.lr_boundarys[0],
                                    weight_decay=self.weight_decay)

        self.scheduler = optim.lr_scheduler.CyclicLR(self.optimizer,
                                                     base_lr=self.lr_boundarys[0], max_lr=self.lr_boundarys[1],
                                                     cycle_momentum=False,
                                                     mode="triangular2",
                                                     step_size_up=7 * int(len(self.loader)))  # 6 * len(self.loader)
        # self.scheduler = optim.lr_scheduler.OneCycleLR(self.optimizer, max_lr=self.lr_boundarys[1],
        #                                              steps_per_epoch=len(self.loader), epochs=self.config["num_epochs"])
        self._RESTART = False
        self.cur_idx = self.dataset.start_index
        self.set_seeds(self.seed)
        if load_from_checkpoint:
            self.load_after_restart()

    def set_seeds(self, seed):
        sys.stderr.write(f"\nSeed: {seed}\n")
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        self.dataset.seed = seed
        if torch.cuda.is_available():
            torch.cuda.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False

    def load_from_checkpoint(self, checkpoint):
        print("=> Loading checkpoint at epoch {} with index {}".format(checkpoint["epochs"][-1], checkpoint["batch_index"]))
        self.model.load_state_dict(checkpoint["state_dict"])
        self.optimizer.load_state_dict(checkpoint["optim_state_dict"])
        self.scheduler.load_state_dict(checkpoint["scheduler"])
        self.logger = checkpoint
        self.dataset.start_index = self.logger["batch_index"]
        self.cur_idx = self.logger["batch_index"]
        self.set_seeds(self.logger["seed"])
        self._RESTART = True
        sys.stderr.write(
            "\n--Loading previous checkpoint--\nID: {}\tEpoch: {}\tBatch_idx: {}\n".format(self.config["track_ID"],
                                                                                           self.logger["epochs"][-1],
                                                                                           self.logger[
                                                                                               "batch_index"]))

    def load_after_restart(self):
        try:
            checkpoint = torch.load(self.config["save_files_path"] + "/checkpoint.pth.tar", map_location=self.device)
            self.load_from_checkpoint(checkpoint)
        except IOError:
            sys.stderr.write("\nNo previous Checkpoint was found, new checkpoints will be saved at: {}".format(
                self.config["save_files_path"] + "/checkpoint.pth.tar"))

        try:
            self.metric_logger = torch.load(self.config["save_files_path"] + "/metrics.pth.tar",
                                            map_location=self.device)
        except IOError:
            sys.stderr.write("\nNo Metric log was found. Evaluation has not been done")

    def save_checkpoint(self):
        self.logger["state_dict"] = self.model.state_dict()
        self.logger["optim_state_dict"] = self.optimizer.state_dict()
        self.logger["batch_index"] = self.dataset.cur_idx#self.cur_idx[-1] + 1 if self.cur_idx[-1] != 0 else 0
        self.logger["scheduler"] = self.scheduler.state_dict()
        self.logger["seed"] = self.dataset.seed
        torch.save(self.logger, self.config["save_files_path"] + "/checkpoint.pth.tar")

    def save_metric_logger(self):
        torch.save(self.metric_logger, self.config["save_files_path"] + "/metrics.pth.tar")

    def restart_script(self):
        from subprocess import call
        self.save_checkpoint()
        sys.stderr.write("\n--Restarting script--\nID: {}\tEpoch: {}\tBatch_idx: {}\n".format(self.config["track_ID"],
                                                                                              self.logger["epochs"][-1],
                                                                                              self.logger[
                                                                                                  "batch_index"]))

        job_name = "id" + str(self.config["track_ID"]).zfill(2) + "e" + str(self.logger["epochs"][-1])
        VRAM = "3.8G"
        if "V6" in self.config["model"] or "V7" in self.config["model"] or "V3" in self.config["model"]:
            VRAM = "7.8G"
        if "V4" in self.config["model"]:
            VRAM = "10G"
        recallParameter = 'qsub -N ' + "id" + str(self.config["track_ID"]) + "e" + str(self.logger["epochs"][-1]) + \
                          str(self.config["model"]) + ' -l nv_mem_free=' + VRAM + " -o " \
                          + str(self.config["save_files_path"]) + "/log_files/" + job_name + ".o$JOB_ID" + " -e " \
                          + str(self.config["save_files_path"]) + "/log_files/" + job_name + ".e$JOB_ID" + ' -v CFG=' \
                          + str(self.config["save_files_path"]) + "/train_config.json" + ' src/train.sge'

        if torch.cuda.is_available():
            sys.stderr.write(f"\nRecall Parameter:\n{recallParameter}")
            call(recallParameter, shell=True)
        else:
            print("Script would have been called:\n" + recallParameter)

    def intermediate_eval(self, num_eval_steps=-1, random_start=True, final=False):
        from subprocess import call
        job_name = "IE" + str(self.config["track_ID"]).zfill(2) + "e" + str(self.logger["epochs"][-1])
        VRAM = "3.8G"
        if "V4" in self.config["model"]:
            VRAM = "5G"
        if "gruV3" in self.config["model"]:
            VRAM = "5G"
        option = ' -l nv_mem_free=' + str(VRAM) if not final else ' -l hostname=vr*'
        recallParameter = "qsub -N " + job_name + self.config["model"] + option  \
                          + " -o " + str(self.config["save_files_path"]) + "/log_files/" + job_name + ".o$JOB_ID" \
                          + " -e " + str(self.config["save_files_path"]) + "/log_files/" + job_name + ".e$JOB_ID" \
                          + " -v STPS=" + str(num_eval_steps) \
                          + " -v RDM=" + str(int(random_start)) \
                          + " -v FNL=" + str(int(final)) \
                          + " -v PTH=" + str(self.config["save_files_path"]) + " src/eval_model.sge"

        if torch.cuda.is_available():
            sys.stderr.write(f"\nRecall Parameter:\n{recallParameter}")
            call(recallParameter, shell=True)
        else:
            print("Script would have been called:\n" + recallParameter)

    def get_starting_parameters(self, what=""):
        if not self._RESTART:
            return 0
        else:
            if what == "running_loss":
                return self.logger["running_loss"]
            elif what == "epoch":
                return self.logger["epochs"][-1]

    def train(self):
        for epoch in tqdm(range(self.get_starting_parameters(what="epoch"), self.config["num_epochs"])):
            sys.stderr.write(f"\nStarting new epoch: {epoch}\n")
            memory = 0
            max_mem = 0
            if not self._RESTART:
                self.logger["epochs"].append(epoch)
                self.logger["lrs"].append(self.optimizer.state_dict()["param_groups"][0]["lr"])
            self.logger["running_loss"] = self.get_starting_parameters(what="running_loss")
            self._RESTART = False
            for i, batch in enumerate(self.loader):
                if self.time_logger.check_for_restart():
                    self.restart_script()
                    return  # End the script

                idx, video_start, (images, labels) = batch
                self.cur_idx = idx
                sys.stderr.write(f"\nCurrent Index: {idx}; dataset idx {self.cur_idx}")

                images, labels = (images.to(self.device), labels.to(self.device))
                if torch.any(video_start):
                    self.model.reset()

                if len(idx) == self.batch_size:
                    if torch.sum(idx == 0) > 1:
                        sys.stderr.write(f"\nEnd reached of batch at index {idx}\n")
                        self.dataset.start_index = 0  # reset start index for the next batch
                        break

                pred = self.model(images)
                loss = self.criterion(pred, labels)
                memory = get_gpu_memory_map()[0] if torch.cuda.is_available() else 0
                max_mem = max_mem if max_mem > memory else memory
                self.optimizer.zero_grad()
                if self.detach_interval == 1:
                    loss.backward(retain_graph=False)
                    self.model.detach()
                elif i > 0 and i % self.detach_interval == 0:
                    loss.backward(retain_graph=False)  # <---------------------------------------------------------
                    self.model.detach()
                else:
                    loss.backward(retain_graph=True)
                self.optimizer.step()
                self.scheduler.step()
                self.logger["running_loss"] += loss.item() * images.size(0)
                self.logger["memory"].append(memory)
                print("Loss: {}, running_loss: {}".format(loss, self.logger["running_loss"]))

            with open(str(self.config["save_files_path"] + "/memory.txt"), "w") as txt_file:
                # max_mem = np.max(np.array(memorys))
                txt_file.write(f"Max cuda memory used in epoch {epoch}: {max_mem}\n")
            self.logger["loss"].append(self.logger["running_loss"] / len(self.dataset))
            visualize_logger(self.logger, self.config["save_files_path"])
            self.save_checkpoint()
            if epoch == self.config["num_epochs"] - 1:
                print("final")
                self.intermediate_eval(random_start=False, final=True)
            elif epoch % self.config["evaluation_steps"] == 0 and epoch > 0:
                print("intermediate")
                self.intermediate_eval(num_eval_steps=29 * 4, random_start=True, final=False)

    def eval(self, random_start=True, eval_length=29 * 4, save_file_path=None, load_most_recent=True):
        import time
        video_freq = 20
        if load_most_recent:
            self.load_after_restart()  # load the most recent log data
        else:
            self.logger["epochs"] = [-1]
        self.dataset.set_start_index(0)
        running_loss = 0
        with torch.no_grad():
            sys.stderr.write("\nEvaluating\n")
            self.model.eval()
            self.model.start_eval()
            metrics = defaultdict(AverageMeter)
            to_PIL = T.ToPILImage()
            if random_start:
                start_index = np.random.choice(range(len(self.dataset) - eval_length))
                self.dataset.set_start_index(int(start_index))
            loader = DataLoader(dataset=self.dataset, batch_size=self.batch_size, shuffle=False, num_workers=0)
            out_folder = Path(save_file_path)
            out_folder.mkdir(parents=True, exist_ok=True)
            mode = "train" if self.dataset.train else "val"
            if self.logger["epochs"][-1] % video_freq == 0 or self.logger["epochs"][-1] == self.config["num_epochs"]-1:
                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                out_vid = cv2.VideoWriter(
                    str(out_folder) + "/intermediate_{}_ep{}.mp4".format(mode, self.logger["epochs"][-1]), fourcc, 29,
                    (1536, 270))
            flickering = 0
            flickering_sum = 0
            flickering_img_size = 0
            fp = 0
            flickering_sum2 = 0
            fip = 0
            for i, batch in enumerate(loader):
                start = time.time()
                idx, video_start, (images, labels) = batch
                if torch.sum(idx == 0) > 1:
                    sys.stderr.write(f"\nlen: {len(self.dataset)}; eval_length: {eval_length}; idx: {idx}\n")
                images, labels = (images.to(self.device), labels.to(self.device))
                if torch.any(video_start.bool()):
                    self.model.reset()
                pred = self.model(images)
                outputs = torch.argmax(pred, dim=1).float()
                end = time.time() - start
                loss = self.criterion(pred, labels)
                # Conversion for metric evaluations
                labels = labels.type(torch.uint8)
                outputs = outputs.type(torch.uint8)
                overall_acc, avg_per_class_acc, avg_jacc, avg_dice = eval_metrics(outputs.to("cpu"),
                                                                                  labels.to("cpu"),
                                                                                  num_classes=2)

                running_loss += loss.item() * images.size(0)
                diff_img = (outputs != labels) * (labels + 1)
                if i > 0:
                    flickering = torch.sum(diff_img != old_out)
                    flickering_sum += flickering
                    flickering_img_size += outputs.shape[1] * outputs.shape[2]
                    fp = float(flickering_sum) / float(flickering_img_size)
                    flickering2 = torch.sum(outputs != old_out2)
                    flickering_sum2 += flickering2
                    fip = float(flickering_sum2) / float(flickering_img_size)

                print(flickering, flickering_sum, flickering_img_size, fp, fip)
                old_out = diff_img
                old_out2 = outputs
                metrics["FP"].update(fp)
                metrics["FIP"].update(fip)
                metrics["hist"].update(fast_hist(outputs.to("cpu"), labels.to("cpu"), num_classes=2))
                metrics["Time_taken"].update(end)
                metrics["Mean IoU"].update(avg_jacc)
                metrics["Pixel Accuracy"].update(overall_acc)
                metrics["Per Class Accuracy"].update(avg_per_class_acc)
                metrics["Dice"].update(avg_dice)

                # conversions since hstack expects PIL image or np array and cv2 np array with channel at last position
                if self.logger["epochs"][-1] % video_freq == 0 or self.logger["epochs"][-1] == self.config["num_epochs"]-1:
                    for j in range(self.batch_size):  # if batchsize > 1 assert that the video writing works
                        out = outputs[j, :, :].unsqueeze(0)
                        lbl = labels[j, :, :].unsqueeze(0)
                        img = images[j, :, :, :].unsqueeze(0)
                        tmp_prd = to_PIL(out[0].cpu().float())
                        tmp_inp = to_PIL(img.squeeze(0).cpu())
                        tmp_inp = Image.fromarray(cv2.cvtColor(np.asarray(tmp_inp), cv2.COLOR_RGB2BGR))
                        tmp_lbl = to_PIL(lbl.cpu().float())
                        out_vid.write(np.array(stack.hstack([tmp_inp, tmp_lbl, tmp_prd])))
                        # break after certain amount of frames (remove for final (last) evaluation)
                if i == eval_length:
                    break
            metrics["eval_loss"].update(running_loss / len(self.dataset))
            metrics["curr_epoch"] = self.logger["epochs"][-1]
            metrics["num_params"].update(sum([param.nelement() for param in self.model.parameters()]))
            if self.logger["epochs"][-1] % video_freq == 0 or self.logger["epochs"][-1] == self.config["num_epochs"]-1:
                out_vid.release()
            self.model.train()
            self.model.end_eval()
            self.metric_logger[mode].append(metrics)
            self.save_metric_logger()


if __name__ == "__main__":
    '''
    Things the Config contains:
    - model (str)
    - batch_size
    - weight_decay
    - unique_name
    - save_files_path
    - save_folder_path
    - track id
    - num epochs
    - evaluation_steps
    '''
    model = "Deep_mobile_lstmV2_2"
    weight_decay = 1e-8
    batch_size = 6
    track_id = 00
    num_epochs = 10
    eval_steps = 2
    unique_name = model + "_wd" + format(weight_decay, ".0e") + "bs" + str(batch_size) + "num_ep" \
                  + str(num_epochs) + "ev" + str(eval_steps) + "ID" + str(track_id)
    config = {
        "save_folder_path": "src/models/trained_models/testing2",
        "save_files_path": "src/models/trained_models/testing2/" + unique_name,
        "model": model,
        "weight_decay": weight_decay,
        "batch_size": batch_size,
        "num_epochs": num_epochs,
        "evaluation_steps": eval_steps,
        "loss": "SoftDice",
        "track_ID": track_id
    }
    Path(config["save_files_path"]).mkdir(parents=True, exist_ok=True)
    with open(str(Path(config["save_files_path"]) / "train_config.json"), "w") as js:  # save learn config
        json.dump(config, js)
    trainer = GridTrainer(config, load_from_checkpoint=False)
    trainer.train()
