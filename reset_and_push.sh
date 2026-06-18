#!/bin/bash
rm -rf .git
git init
git remote add origin https://github.com/hwangsueun/stock-game-origin.git
git add .
git commit -m "Initial commit: code only"
git push -u origin master:main --force
