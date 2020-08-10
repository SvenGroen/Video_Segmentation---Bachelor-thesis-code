import argparse
import json

from src.gridtrainer import GridTrainer

parser = argparse.ArgumentParser()
parser.add_argument("-cfg", "--config",
                    help="The Path to the configuration json for the model.\nShould include: model, ID, lr, batchsize,"
                         " num_epochs, scheduler_step_size, save_freq, save_path", type=str)
args = parser.parse_args()
if args.config is not None:
    with open(args.config) as js:
        print("Loading config: ", args.config)
        config = json.load(js)

trainer = GridTrainer(config)
trainer.train()