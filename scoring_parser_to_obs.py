from ScoringParser import ScoringParser
import yaml
import time

yaml_file_name = 'scoring_parser_config.yaml'

with open(yaml_file_name, 'r') as yfile:
    sp_config = yaml.safe_load(yfile)
    
scoring_parser = ScoringParser(sp_config)

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass