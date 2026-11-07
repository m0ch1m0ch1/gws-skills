# gws-skills

Google Workspace を [gws](https://github.com/googleworkspace/cli) (Google Workspace CLI) で操作する Claude Code 用のスキル集です。

## スキル

| スキル | できること |
| --- | --- |
| [gws-md2gdoc](.claude/skills/gws-md2gdoc/README.md) | Markdown (手順書など) を、読みやすく組み直して Google ドキュメントにする |
| [gws-yaml2gsheet](.claude/skills/gws-yaml2gsheet/README.md) | フォルダに入れた YAML (設定値や Kubernetes のマニフェストなど) を、Google スプレッドシートのパラメータシートにする |
| [gws-meeting-history](.claude/skills/gws-meeting-history/README.md) | 会議の名前から、その打ち合わせの今日までの経緯・課題と宿題・今後の予定を、カレンダーと直近の資料から要約して会話に示す |

## 使い始め方

1. 各スキルの README の前提条件をそろえる (gws のインストールと `gws auth login` での認証など)。
2. このリポジトリをクローンし、ルートで Claude Code を起動する。スキルは `/<スキル名>` で実行する (例: `/gws-md2gdoc`)。
3. ファイルを渡すスキルでは、ファイル (スキルによってはフォルダ) を `input/<スキル名>/` に置く。置き方は各スキルの README を参照。

## 構成

```text
.
├── .claude/
│   ├── settings.json         # 共有の許可。スキルのスクリプトと、Google Workspace を読むだけの操作を確認なしで実行する (認証情報は読ませない)
│   └── skills/<スキル名>/     # 各スキル
├── .github/                  # CI (検査は scripts/check.sh)、Issue と PR のテンプレート
├── CLAUDE.md                 # Claude Code が作業するときの決まり
└── input/<スキル名>/          # スキルに渡すファイル (Git 管理対象外)
```

`.claude/settings.json` の許可は、リポジトリのルートからスキルを実行する前提で書いています。

## 検査

`bash .github/scripts/check.sh` で、CI と同じ検査を手元で実行できます。

## ライセンス

[MIT](LICENSE)
