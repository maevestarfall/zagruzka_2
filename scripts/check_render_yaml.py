#!/usr/bin/env python3
# scripts/check_render_yaml.py
import yaml
import os
import sys

def main():
    print("Verifying render.farm.yaml...")
    if not os.path.exists("render.farm.yaml"):
        print("Error: render.farm.yaml does not exist.", file=sys.stderr)
        sys.exit(1)
    with open("render.farm.yaml", "r") as f:
        data = yaml.safe_load(f)
    if "services" not in data:
        print("Error: render.farm.yaml lacks 'services' key.", file=sys.stderr)
        sys.exit(1)
    print("render.farm.yaml verification passed successfully.")

if __name__ == "__main__":
    main()
