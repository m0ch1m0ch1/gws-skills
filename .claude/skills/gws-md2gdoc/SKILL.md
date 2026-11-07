---
name: gws-md2gdoc
description: MarkdownからGoogleドキュメントを作る（gwsを使う）。
allowed-tools: AskUserQuestion
---

# gws-md2gdoc

`T` は `python3 -I .claude/skills/gws-md2gdoc/scripts/md2gdoc.py` の略で、リポジトリのルートからこの形のまま実行する。下の 1〜3 を順に行い、出力の `[NG]` と `案内:` に従う。ユーザーに聞くところは AskUserQuestion を使い、回答なしに進めない。

`T create` は、同じ入力とオプションで実行した `T plan` の出力を示して、作ってよいと確認を得てから実行する。何かを変えたら `T plan` からやり直す。

## 1. plan

`T plan [<入力>]` を実行する。`<入力>` は引数のファイル名で、無ければ省く。0 以外なら `案内:` に従って直し、`T plan` をやり直す。認証が要るときは、ユーザーに `! gws auth login` の実行を依頼する。自分で `gws auth` を実行しない。

## 2. 確認

`T plan` の出力をそのまま示し、この内容 (特に保存先) で作ってよいかを確認する。変えたいと言われたら、次を足す。

- ファイル名: `--name <名前>` (`yyyymmdd_` より後)
- 保存先: `--folder <フォルダの URL>` (引数か依頼にフォルダの URL があれば、最初から足す)

## 3. create

`T create [<入力>] [--name <名前>] [--folder <フォルダの URL>]` を実行する。

- 終了コード 0: 最後の行の JSON の `url` (ドキュメント) と、`folder`・`folderUrl` (保存先) を伝える。
- 0 以外: `[NG]` の内容を伝える。`[OK] ドキュメントを作成` の後に失敗したなら、途中までのドキュメントがドライブに残っていることも伝える (自分で削除しない)。
