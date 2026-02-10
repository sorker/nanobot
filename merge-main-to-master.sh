#!/usr/bin/env bash
# 将远程 main 拉取并合并到当前分支（多为 master）
# 若有冲突，请手动解决后执行: git add . && git commit

set -e

echo "==> 1. 拉取远程 main..."
git fetch origin main

echo ""
echo "==> 2. 检查本地未提交修改..."
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "存在未提交修改，先暂存 (git stash)..."
  git stash push -m "WIP before merge origin/main"
  STASHED=1
else
  STASHED=0
fi

echo ""
echo "==> 3. 合并 origin/main 到当前分支..."
if git merge origin/main -m "Merge origin/main into master"; then
  echo ""
  echo "合并完成，无冲突。"
  [ "$STASHED" = 1 ] && echo "恢复暂存: git stash pop"
else
  echo ""
  echo "存在冲突，请手动解决以下文件中的冲突标记后执行:"
  echo "  git add ."
  echo "  git commit"
  echo ""
  git status --short
  [ "$STASHED" = 1 ] && echo "" && echo "合并提交完成后，可用 git stash pop 恢复之前暂存的修改。"
  exit 1
fi
