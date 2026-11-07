#!/usr/bin/env python3
"""Render/verify the separately retained queue-actor campaign; never pool runs."""
import argparse
from plot import ROOT, render, verify

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    (verify if args.verify else render)(ROOT/'results/osb/isolated-campaign', ROOT/'figures/isolated')
