#!/usr/bin/env python3
# scripts/gen_fly_toml.py

def main():
    print("Generating fly.toml for overflow gate...")
    toml_content = """
app = "lilush-coder-overflow"
primary_region = "ams"

[env]
  LILUSH_INSTALL_ROOT = "/data"
  LILUSH_CODER_OVERFLOW = "fly"
"""
    with open("fly.toml", "w") as f:
        f.write(toml_content.strip())
    print("Successfully generated fly.toml.")

if __name__ == "__main__":
    main()
