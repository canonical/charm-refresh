#!/bin/bash
set -e

# Configure git to fetch all branches (not the default on Read the Docs)
git config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'
# Fetch all branches & tags
git fetch --tags

npm install
npx antora antora-playbook.yml --to-dir "$READTHEDOCS_OUTPUT/html"
