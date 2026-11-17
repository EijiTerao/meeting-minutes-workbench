# Codex・Claude Codeに導入を頼む

すでに利用しているCodexまたはClaude Codeで、ダウンロード・展開したフォルダを開き、「このアプリをこのPCに導入して」と頼みます。Codexは [AGENTS.md](AGENTS.md)、Claude Codeは [CLAUDE.md](CLAUDE.md) から同じ手順を参照します。別のAIを起動したり、アプリ用にAPIキーを作ったりする必要はありません。リポジトリをまだ開いていない場合は、次の文を使ってください。

> EijiTerao/meeting-minutes-workbench を、利用者である私のPCに導入してください。あなたは導入を手伝うAIで、リポジトリの配布者や公式サポートではありません。READMEとこの手順を読み、私のOS・CPUと既に使える処理方法を確認してください。追加のAPI課金がない構成を選び、初期設定まで済ませ、合成サンプルで起動と出力を確かめてください。アプリの設定画面で同じ項目を選び直す必要がないようにしてください。追加のインストール、実データをAIサービスへ渡す操作、録音権限の設定は、必要なものだけこの会話で確認してください。OSの保護を弱めたり、無断で録音を始めたり、APIキーを探したりしないでください。

## AIが確認する順序

1. GitHubの **Code → Download ZIP** か `git clone` でソースを取得します。公式のリポジトリを使い、既存の作業フォルダは上書きしません。
2. Apple Silicon・macOS 26以降なら、[READMEの導入手順](README.md#導入と最初の起動)に沿って、利用者のMac上でアプリを作ります。ビルド前に `python3 -B tools/build_public_macos.py --check` を実行し、前提が揃っていれば `--out outputs/mac-local` で作成します。完成済みアプリの配布ZIPは探しません。
3. ビルドに必要なものが足りない場合は、無断で開発ツールをインストールしたり、利用者のデータを消したりしません。PythonとTkが使えるなら下の起動方法を案内し、Mac用アプリとは区別します。画面を開けない場合は、できた工程とFinderで開くパスを伝えます。
4. 初回は合成サンプルだけで確認します。実際の収音は、利用者が収音対象を確認し、必要なmacOSの許可を付けてから行います。実データのAI送信は導入に含めず、利用者が用途・送信先・内容を確認してから実行します。

### Windows・Linux、またはMacでネイティブ版を作れないとき

```sh
python3 -m minutes setup --check
python3 -m minutes setup
python3 -m minutes gui
```

Windowsでは `python3` を `py -3` に置き換えます。既存の `whisper-cli` と対応するモデルを確認できれば、端末内文字起こしを選びます。モデルを安全に特定できないときはTXT取り込みで始めます。利用者がすでに使っている処理を選んだ場合だけ、たとえば `setup --provider ollama --model モデル名` で議事録生成を設定します。既存設定を変更する必要がある場合は、違いを示して本人が選んだ設定だけを変更してください。

短時間だけ動作を確かめる場合は、`gui --settings PATH --check-launch` でウィンドウの表示と設定の再読込を確認できます。検証用の設定は `setup` にも同じ `--settings PATH` を渡します。CLIの隔離環境で画面を開けない場合は、権限を緩めず、そのままの設定で通常のデスクトップから起動します。

### 追加のAPI料金がかからない使い方

- **端末内で完結:** Mac用アプリのApple音声認識・端末内AI、または導入済みのローカルモデルを使います。設定時にソフトやモデルを入れたり、AIサービスへ接続したりしません。Ollamaを選ぶ場合は、[公式手順](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features)でクラウド機能を無効にし、既に端末にあるローカルモデルだけを使います。
- **契約中のAIを利用者が使う:** 「他のAI用の指示をコピー」などの操作は、クリップボードに置くだけです。利用者が文字起こしやメモの送信範囲を確認し、契約中のChatGPT／CodexやClaudeなど、公式サービスの自分のセッションへ貼り付けた時点で、そのサービスに渡ります。リポジトリの配布者には送りません。アプリはサービスにログインせず、認証情報を扱いません。
- **契約に含まれる枠だけを使う:** [ChatGPTとOpenAI APIの請求](https://help.openai.com/en/articles/9039756-managing-billing-for-chatgpt-and-the-api-platform)は別です。[Codex](https://help.openai.com/en/articles/20001275-chatgpt-work-and-codex)はChatGPTアカウントでの利用とAPIキーでの利用が別で、購入クレジットや従量課金枠を使うと追加費用が生じる場合があります。[Claude Code](https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-max-or-ultra-plan)も、APIキーがサブスクリプションのログインより優先される場合があります。契約内であることを確認できないときや枠を使い切ったときは、課金経路に切り替えず、手元の処理に戻るか枠の回復を待ちます。

この導入手順はOpenAI／Anthropicの従量課金APIを設定・実行しません。既存の設定に課金APIがあっても変更せず、導入検査では使いません。標準では原音を残さず、文字起こしだけを保存します。

### 導入に使ったAIで議事録を作る

利用者がこのAIサービスで処理してよいと判断した文字起こしTXTだけを指定し、「このTXTから、本文と未決事項を別々に作って」と頼みます。同じ公式Codex／Claude Codeセッションで議事録を作り、ローカルの整形コマンドで `minutes.txt` と `open-issues.txt` に保存します。アプリはサブスク認証を代行せず、課金APIにも切り替えません。処理の指示は [AGENTS.md](AGENTS.md) にあります。

AIにローカルファイルを読ませる場合も、そのAIサービスで内容が処理されます。端末内に留めたいデータは、Appleの端末内AI、ローカルモデル、手入力を選んでください。[Claude Codeの公式方針](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use)に従い、公開版からClaudeアカウントに代理ログインしたり、第三者アプリのバックエンドとして認証を流用したりしません。
