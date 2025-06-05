#!/bin/bash
# Renaming this file requires changes to .readthedocs.yaml on all actively maintained source
# code/documentation content branches. (The .readthedocs.yaml file on this branch [which contains
# the Antora playbook and does not contain source code/documentation content] does not need to be
# modified if this file is renamed.)

set -e

if [[ $READTHEDOCS_GIT_IDENTIFIER == "main" || $READTHEDOCS_GIT_IDENTIFIER =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
then
  echo 'Documentation build should not be triggered on release branches; trigger on docs-playbook branch instead'
  # https://docs.readthedocs.com/platform/stable/build-customization.html#cancel-build-based-on-a-condition
  exit 183
fi

# Fetch pull request to branch (e.g. for PR #3, fetch pull request to "pr-3" branch)
# "$READTHEDOCS_GIT_IDENTIFIER" is the PR number
# (https://docs.readthedocs.com/platform/stable/reference/environment-variables.html#envvar-READTHEDOCS_GIT_IDENTIFIER)
git fetch origin "pull/$READTHEDOCS_GIT_IDENTIFIER/head:pr-$READTHEDOCS_GIT_IDENTIFIER"

# Install yq
mkdir -p ~/.local/bin/
wget --no-verbose https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -O ~/.local/bin/yq
chmod +x ~/.local/bin/yq

# Add the pull request branch to the Antora playbook
~/.local/bin/yq --inplace ".content.sources[0].branches += [\"pr-$READTHEDOCS_GIT_IDENTIFIER\"]" antora-playbook.yml
cat antora-playbook.yml

./build.sh
