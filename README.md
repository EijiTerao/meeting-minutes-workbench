# Meeting Minutes Workbench

対面の会話はマイクで、ZoomやGoogle Meetなどのオンライン会議はパソコンの再生音から、文字起こしを手元に残すデスクトップアプリです。Mac版では、イヤホンで会議を聞きながら、マイクをオフにして再生音だけを取り込めます。

標準は、原音を残さず文字起こしだけを保存する設定です。あとで聞き直したい会議は、原音も残せます。必要なら、文字起こしから議論の流れ・決定・担当・期限をまとめ、共有する本文と未決事項を分けて取り出せます。

[Codex・Claude Codeに導入を頼む](AI-SETUP.md) · [録音方法を選ぶ](#これから録音する) · [録音ファイルから文字にする](#すでに録音がある) · [議事録の設定](#自分の会議で使う)

## 導入と最初の起動

### Mac：ソースをダウンロードして、自分のMacでアプリを作る

このリポジトリは**ソースコードを公開し、完成済みのMacアプリは配布しません**。対象はApple SiliconのMac・macOS 26以降です。ビルドには、macOS 26以降のSDKとSwift 6.2以降が使えるXcodeまたはCommand Line Tools、Python 3.11以降、1 GiB以上の空き容量が必要です。

1. GitHubの **Code → Download ZIP** からソースを取得して展開します。Gitを使うなら `git clone https://github.com/EijiTerao/meeting-minutes-workbench.git` でも取得できます。
2. 展開したフォルダをCodexやClaude Codeで開き、「このMacでアプリを作って」と頼みます。手元のターミナルで行う場合は、`README.md` のあるフォルダで次を実行します。

```sh
python3 -B tools/build_public_macos.py --check
python3 -B tools/build_public_macos.py --out outputs/mac-local
```

3. Finderで `outputs/mac-local/Meeting Minutes Workbench.app` を開きます。更新を試す場合は、`--out` に別のフォルダ名を指定してください。既存のアプリや会議データは上書きしません。

作成したMacで使うためのローカル署名はビルド時に付きますが、**配布用のDeveloper ID・Appleの公証・アプリ用APIキーは不要**です。起動後のアプリにPythonやブラウザは不要です。開けない場合はOSの保護設定を変えず、表示されたエラーと `--check` の結果を確認してください。

実際に録音するには、収音方法に応じて[macOSの収音許可](https://support.apple.com/guide/mac-help/control-access-screen-system-audio-recording-mchld6aa7d23/mac)と、必要に応じてAppleの音声認識データが要ります。ローカルで作り直した後に、macOSから許可を再度求められることがあります。

### Windows・Linuxのデスクトップ画面をソースから開く

Python 3.11以上で動きます。サンプルを開くだけなら、追加のPythonパッケージ、AIアカウント、APIキー、Obsidianは不要です。使用できる機能は起動方法によって異なります。

1. GitHubのこのページで **Code → Download ZIP** を選び、ZIPを展開します。Gitを使う場合は `git clone https://github.com/EijiTerao/meeting-minutes-workbench.git` でも取得できます。
2. `README.md` と `minutes` フォルダが見える場所でターミナルを開きます。Windowsではエクスプローラーのアドレスバーに `powershell` と入力すると、その場所で開きます。macOSではターミナルで `cd ` と入力したあと、展開したフォルダをドラッグしてReturnを押します。
3. OSに合った起動コマンドを実行します。

   | OS | バージョン確認 | 起動 |
   |---|---|---|
   | macOS・Linux | `python3 --version` | `python3 -m minutes` |
   | Windows | `py -3 --version` | `py -3 -m minutes` |

   Pythonが見つからない、または3.11未満なら、[Python公式配布](https://www.python.org/downloads/)を確認してください。Windowsで `py` がなくても、`python --version` が3.11以上を示す場合は `python -m minutes` で起動できます。

4. 初回は利用できるローカル処理で起動します。AIに導入を頼む場合は、環境の確認と設定も [AI向け導入手順](AI-SETUP.md) に沿って進められます。**構造化JSONを開く** から `examples/minutes.json` を選ぶと、架空の会議の **本文をコピー** と **未決事項をコピー** を別々に試せます。

画面が開かないときは `python3 -m tkinter`（Windowsでは `py -3 -m tkinter`）でTkの有無を確認できます。Tkがない環境でも、同じ場所で次のコマンドを実行するとサンプルを整形・保存できます。

```sh
python3 -m minutes render examples/minutes.json --title "図書室の案内改善" --out ../minutes-demo
```

Windowsでは、上の `python3` を `py -3` に置き換えてください。`minutes.txt` と `open-issues.txt` を新しいフォルダに作ります。既存ファイルは上書きしません。

## 自分の会議で使う

Mac用SwiftUIアプリでは、作業タブで録音するか「取り込む」から文字起こしTXTを開きます。左の「作成」から、利用可能ならAppleの端末内AIで議事録を作れます。端末内AIを使えない場合や長い原文の場合は、別のAIに貼り付ける指示文をコピーするか、手入力できます。「本文」「未決事項」は別々にコピー・書き出しできます。「分類文脈」で話題を入れた会議は、履歴で話題ごとに並びます。参照をオンにすると、同じ話題で保存した過去議事録の抜粋を端末内AIだけで利用します。

以下の処理ごとのAI設定・高品質モードは、`python -m minutes` で起動する版の機能です。SwiftUI版の「作成」では外部AIへ自動送信せず、Obsidianとの連携設定もありません。

| 工程 | 選べる処理 |
|---|---|
| 文字起こし | テキスト／字幕の取り込み、手元の録音を whisper.cpp で文字起こし |
| 文字起こしの校正 | 原文のまま、導入済みのローカルOllama |
| 議事録生成 | 手動で構造化JSONを読み込む、導入済みのローカルOllama |
| 生成結果の根拠確認 | 追加のAI確認なし、導入済みのローカルOllama |

初期設定は端末内で可能な文字起こし、またはTXT取り込みから始めます。生成方法の追加は、[AIに設定まで頼む](AI-SETUP.md) か、アプリの「処理の設定」から変更できます。まず「議事録生成」だけを有効にし、誤認識が目立つときに校正を、重要な会議で根拠を照合したいときに確認を加えてください。

- **Ollama:** [公式手順](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features)でクラウド機能を無効化し、起動済みのローカルOllamaに準備済みのモデル名を指定します。[構造化出力](https://docs.ollama.com/capabilities/structured-outputs)に対応するモデルを使用してください。
- **すでに契約しているAI:** 「外部AI用の指示をコピー」は、文字起こしと今回の確認済み情報をクリップボードに置きます。送信先と内容を確認し、利用者自身の公式AIセッションに貼り付けた場合だけ、そのサービスへ渡ります。配布者への送信はありません。返されたJSONを「構造化JSONを開く」から読み込み、出力を原文と照合してください。契約に含まれる枠だけで使う方法は[AI向け導入手順](AI-SETUP.md)を参照してください。
- **OpenAI／Anthropicの直接API:** ソース起動版には従来の明示設定が残っていますが、上の利用方法とは別の、従量課金があり得る経路です。初回セットアップはこれらのAPIを設定・実行しません。追加料金なしで使いたい場合は選ばないでください。

タイトルと文字起こしを入力します。任意で参考資料や、人が確認した担当・予定などを追加してから生成します。完成後は「本文」「未決事項」を別々に直し、コピーまたは保存できます。重要な担当・日付・数字は共有前に原文と照合してください。

- **会議タブ**：別の会議を開いても、元のタブの処理は続きます。「生成を中断」は次の送信と結果の反映を止めます。すでに送信したリクエストは取り消せません。「文字起こしをコピー」は選択部分を優先し、選択がなければ全文をコピーします。分割文字起こし中は、最後に保存を確認できた途中TXTを取得できます。
- **話題別の履歴**：「話題」を入れ「履歴に保存」を押した会議だけが端末内の履歴に残ります。一度保存した会議では、そのタブで生成を完了すると同じ履歴に保存します。「同じ話題の過去議事録を参照」は初期状態でオフです。有効にすると、同じ話題の過去議事録の本文から短い抜粋だけを背景に使い、過去の文字起こしや別の話題は渡しません。外部AIを選んだ場合は、実行前に対象の議事録名も表示します。Obsidianは不要です。
- **高品質モード**：今回の出席者や担当など、解釈によって議事録が変わる点があれば「確認の質問に答える」に表示します。回答を使って再生成するか、回答せず下書きを使うかを選べます。質問がなくても「確認済み事実を追加」から入力できます。

### これから録音する

Mac用アプリでは「対面」または「オンライン」を選び、「録音を開始」を押します。参加者に収音と保存範囲を確認してから開始してください。「オンライン」はMacの再生音を取り込むため、ZoomやMeetの指定は不要ですが、通知音や他のアプリの音も入り得ます。マイクは初期状態でオンです。「収音設定」の「相手の音声のみ（マイクOFF）」にチェックを入れると、マイクを使わずに再生音だけを取り込みます。初回は選んだ収音方法に応じ、macOSからマイクまたは画面・システムオーディオ収録の許可を求められる場合があります。映像は保存しません。

- **文字起こしのみ（標準）**：音声は区間ごとに端末内で処理し、TXTだけを保存します。原音は後から聞き直せません。Apple音声認識を使う場合は、対応する認識データがMacに入っている必要があります。
- **原音も保存する（任意）**：指定した録音だけ、文字起こしと一緒に分割WAVを保存します。「録音停止」後は、末尾の文字起こしと保存を待ちます。

オンラインの録音を開始して「TCCを拒否」と表示された場合は、macOSの「システム設定」→「プライバシーとセキュリティ」→「画面収録とシステムオーディオ録音」で **Meeting Minutes Workbench** の許可を確認し、アプリを開き直してください。「相手の音声のみ」の場合は、アプリにマイクの許可を付ける必要はありません。

#### ソースから起動した場合のブラウザ録音（補助機能）

Mac用アプリを使わずソースから起動すると、「ブラウザ録音ページ（非配布版）」から補助の録音ページを開けます。Mac用アプリの標準経路とは別です。

- **対面**：マイクを録ります。
- **オンライン**：Mac・Windowsとも、初期状態は「PCの再生音」と「マイク」の両方です。ZoomやMeetを指定する必要はありません。通知音や他のアプリの音も入り得ます。マイクを入れたくないときは「マイクも録る」をオフにします。ブラウザで会議をしていて、そのタブの音だけに絞りたい場合は「共有するタブの音声だけに絞る」を選びます。

オンラインで録音を始めると、ブラウザの共有画面が開きます。標準設定では画面全体、タブに絞った場合は対象のタブを選び、「音声を共有」をオンにしてください。映像は保存しません。

- **文字起こしのみ（標準）**：音声を短い区間ごとに端末内で処理し、確定した文字だけをTXTに保存します。原音ファイルは作らないため、あとから音声の聞き直しはできません。保存先は通常 `~/.meeting-minutes-workbench/live-transcripts/` です。停止後は末尾の処理と保存を待ちます。文字起こしが止まっても、保存できた区間は残ります。
- **原音も保存する（任意）**：聞き直したいときだけオンにします。「長時間モード」では保存先を準備すると、原音を5秒ごとにWAVへ保存します。「短時間モード」では最大15分まで録り、停止後にWAVを手動で保存します。

文字起こしには端末内の認識器が必要です。Mac・Windowsで、すでに用意した `whisper-cli` とモデルをアプリの「処理の設定」で選べます。認識器が使えない状態で「文字起こしのみ」は開始しません。原音の保存へ自動では切り替わりません。HTMLファイルを直接開いた場合は文字起こしに接続できないため、明示的に原音保存を選んだ場合だけWAVを録音できます。

Macで認識データがすでに入っている場合は、macOS 26以降と対応SDKでAppleの日本語・韓国語認識も選べます。次のコマンドで補助ツールをビルドし、その実行ファイルを指定してアプリを起動します。アプリからモデルはダウンロードしません。

```sh
swiftc -parse-as-library native/macos/TranscribeAudio.swift -o ../workbench-apple-transcribe
MINUTES_APPLE_TRANSCRIBER="$(pwd)/../workbench-apple-transcribe" python3 -m minutes
```

画面全体の再生音を共有できるかは、OS・ブラウザの組み合わせによります。共有画面で音声が選べない場合や、ブラウザが音声を渡さない場合は開始しません。別の画面種別やマイクだけに自動で切り替えることもありません。再生音だけを録りたい場合は、以下のMac・Windows用補助ツールも利用できます。収音前に会議アプリの出力先を確認してください。

#### Macで再生音を取る（ブラウザが音声を共有できない場合など）

macOS 13以降と利用可能なSwiftコンパイラがある場合は、[ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit/capturing-screen-content-in-macos)を使う補助ツールを手動起動できます。不要ならこの手順は飛ばしてください。マイクや映像は保存しません。

```sh
swiftc -parse-as-library native/macos/SystemAudioCapture.swift -o ../workbench-system-audio
../workbench-system-audio --output ../recording-001 --confirm --whole-mac
```

特定のアプリだけに絞りたい場合は、`--whole-mac` を `--app-bundle-id '対象のBundle ID'` に置き換えます。Bundle IDは対象アプリの `.app` について `mdls -name kMDItemCFBundleIdentifier -raw` で確認できます。対象アプリは同じログインセッションで開いてください。ブラウザを指定すると、そのブラウザ全体が対象です。Meetタブだけに絞りたい場合は、録音ページでタブに絞ってください。

`--whole-mac` では他のアプリの音も入り得ます。複数画面での取り込み範囲は未検証です。まだ存在しない保存先を指定し、音声収録に必要なmacOSの許可は実行する利用者が判断してください。停止はCtrl+Cで行い、完了の表示を確認します。保存先は「分割WAVを文字起こし」で開けます。

#### Macで原音を残さず文字起こしする（ブラウザが再生音を共有できない場合）

上のMac用補助ツールと、標準入力からWAVを読める手元の [whisper.cpp](https://github.com/ggml-org/whisper.cpp) がある場合に使えます。音声は端末内のメモリから認識器に渡し、ディスクには文字だけを保存します。`--confirm` は参加者への同意と収音範囲を確認してから指定してください。

```sh
python3 -m minutes.live_transcript \
  --capture '/path/to/workbench-system-audio' \
  --whisper '/path/to/whisper-cli' --model '/path/to/ggml-base.bin' \
  --output '/path/to/new-live-transcript.txt' --language ja \
  --whole-mac --confirm
```

追記先が作成されたら、議事録画面で「TXTを追う」を押して選ぶと、保存された区間から表示・コピーできます。Ctrl+Cで停止し、認識・文字保存が終わるのを待ちます。認識が収音に追いつかなくなった場合や保存に失敗した場合は停止し、既に保存した文字を残します。音声を保存するモードに自動では切り替わらず、取り逃した音は復元できません。

#### Windowsでイヤホンやスピーカーの再生音を取る（任意）

ブラウザが共有音声を提供しないときは、Windows PowerShellが使える環境で、[WASAPIのループバック](https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording)を使う補助ツールを選べます。会議アプリが実際に再生しているイヤホンやスピーカーを指定します。**その出力先に流れる他のアプリや通知音も入ります。** マイク・動画は取得せず、会議アプリだけに絞る機能はありません。

```powershell
powershell -NoProfile -File .\native\windows\Capture-SystemAudio.ps1 -ListEndpoints
powershell -NoProfile -File .\native\windows\Capture-SystemAudio.ps1 -EndpointId '<選んだ出力先のID>' -OutputDirectory '<空の既存フォルダ>'
```

対象と同意を確認して `CAPTURE` と入力します。停止はCtrl+Cです。停止後は「分割WAVを文字起こし」からこのフォルダを読み込めます。途中でイヤホンを外すなどして選んだ出力先が失われた場合は、別の出力先へ自動で切り替えません。組織や端末の実行ポリシーでスクリプトを開けない場合は、その設定を変えずブラウザ経路を利用してください。Windowsの実機収音は未検証です。

### すでに録音がある

任意で [whisper.cpp](https://github.com/ggml-org/whisper.cpp#quick-start) の `whisper-cli` と多言語モデルを用意し、「処理の設定」で文字起こしを有効にします。単一の録音は「WAVを文字起こし」、長時間モードの録音は「分割WAVを文字起こし」から、停止後のセッションフォルダを選びます。対応入力は16kHz・16bit・モノラルPCMのWAVです。

選んだ保存先へ、認識できた区間を約5分ずつ `transcript.partial.txt` のような途中ファイルに残します。最後まで完了したときだけ `transcript.txt` を作ります。途中で止まった場合は、同じ音声・認識器・モデル・言語で同じ出力名を選ぶと、保存済み区間を読み戻して再開します。途中TXTの隣にある `.asr-checkpoint` フォルダも残してください。元の音声と別のジョブの出力は上書きしません。正常終了の印がない録音は確認後に読み込めますが、保存されなかった区間は復元できません。欠番・読めないWAVがある場合は黙って飛ばさず停止します。

```sh
python3 -m minutes transcribe meeting.wav --whisper "/path/to/whisper-cli" --model "/path/to/ggml-base.bin" --out transcript.txt
```

分割録音では上の `meeting.wav` を選んだセッションフォルダに替えます。完了の印がないフォルダで、残った音声を意図して処理するときだけ `--allow-partial-recording` を追加してください。

Windowsでは選んだ `whisper-cli.exe` とモデルのパスを指定してください。選んだ実行ファイルを起動するため、入手元が信頼できるものだけを指定してください。このアプリによるインストール、モデル取得、実行ファイルの真正性検証は行いません。

#### Macに入っているApple音声認識を使う（任意）

macOS 26以降とmacOS 26以降のSDKがあり、対応する日本語・韓国語の認識データがすでに入っている場合は、Whisperを追加せずに単一WAVや録音完了済みの分割WAVからTXTを作れます。[Apple SpeechTranscriber](https://developer.apple.com/documentation/speech/speechtranscriber)で端末内処理し、認識データがなければ処理を止めます。アプリからはモデルの取得もマイク使用も行いません。

```sh
swiftc -parse-as-library native/macos/TranscribeAudio.swift -o ../workbench-apple-transcribe
../workbench-apple-transcribe --input ../recording-001 --output ../transcript.txt --language ja
```

生成されたTXTは「テキストを開く」から読み込めます。この経路は録音後の文字起こし用です。

コマンドで同じ工程を使うには、`examples/pipeline-config.json` を配布元とは別の場所にコピーし、使う工程だけを設定します。

```sh
python3 -m minutes workflow examples/benchmark-transcript.txt --title "展示会の準備" --settings ../pipeline.local.json --out ../minutes-candidate
```

`--reference` で参考資料、`--confirmed` で確認済みテキストを指定できます。`--wav` は単一WAV用です。長い録音は、先に `transcribe` で文字起こしを保存しておくと、議事録生成で失敗しても原文を再利用できます。クラウドを利用する設定は、今回の送信とAPI利用を許可する `--allow-openai` または `--allow-anthropic` が該当サービスごとに必要です。各工程の時間は `timing.json` に保存します。校正を採用した場合も原文は上書きしません。

## 書式を変える

標準では、本文は【決定事項】【担当】【期限】【次回】の順です。内容がある章だけ表示し、＜テーマ＞の下に「・」で具体的な文章を並べます。次回開催の情報は【次回】にまとめ、まだ決まっていない実質的な論点は【未決事項】に分けます。

```text
図書室の案内改善

【決定事項】
＜返却棚の案内＞
・ 返却棚の場所で迷う人を減らすため、入口と階段横の2か所に同じデザインの案内を掲示する。
```

`examples/profile.json` をコピーし、画面で選ぶかコマンドに `--profile` で渡してください。`labels` は章名、`order` は本文の章順、`bullet` は箇条書き記号、`section_brackets` と `topic_brackets` は見出しを囲む記号です。`style_instructions` には「1項目は1文」「社内用語は変えない」などを指定できます。キーを省略すると既定値を使います。

## 保存とプライバシー

- アプリは他のアプリの会議履歴・カレンダー・チャット・Vaultを探索しません。SwiftUI版は `~/Library/Application Support/MeetingMinutesWorkbench/Meetings` に会議別のTXTを、原音を有効にしたときだけWAVを保存します。`python -m minutes` 版の基本の履歴保存先は `~/.meeting-minutes-workbench/meetings.sqlite3` です。いずれもアプリ独自の暗号化は行いません。
- 設定ファイルに保存するのは方法・モデル名・選んだローカル実行ファイルやモデルのパスだけです。APIキーや会議の中身は設定に保存しません。
- ローカルAIは `127.0.0.1:11434` です。外部AIは今回の送信に許可した処理だけが、選択に応じて `api.openai.com` または `api.anthropic.com` に接続します。録音ファイルは送りません。失敗しても別の接続先へ切り替えません。
- 参考資料は用語・背景の補助です。会議で決まったことや参加者を補完する根拠にはしません。ただし、生成モデルの事実誤りを完全には防げません。必要に応じて元の文字起こしと照合してください。
- アプリが会議データを公開することはありません。標準の文字起こしは端末の `live-transcripts` フォルダへ、原音を残す場合は利用者が選んだ保存先へ保存します。保存先の同期・閲覧権限や、コピー先アプリは利用者が確認してください。参加者の同意と利用範囲を確認した内容だけを扱ってください。
- ブラウザの文字起こしは、「録音ページを開く」を押したときだけアプリが同じPC内の `127.0.0.1` に用意する接続を使います。原音は認識器にメモリから渡し、外部へ送信しません。通常の議事録生成でオンラインAIを選んだ場合の送信とは別です。

## 対応範囲

- Mac用アプリはアプリ内で録音と文字起こしを行います。Meet接続中、Bluetoothイヤホン・マイクOFFで文字起こしTXTの保存までは確認しました。相手の発話が正確に入るかの照合と、Zoomの相手の発話を使った検証は未実施です。
- Windowsは同じ左右の画面構成で、収音は対応ブラウザの録音ページまたはWASAPI補助ツールを使います。Macとは録音経路が異なります。話者の自動認識やカレンダー連携はありません。
- Windows・macOS・LinuxのPython標準ライブラリを使う設計です。Tkの有無と任意モデルの利用条件は環境によって異なります。
- Windowsは自動テストの対象です。Windows実機での画面・収音は未確認です。従来の個人用アプリとの出力・速度の同等性は保証しません。
- `workflow` は、最大192KBの文字起こしを分割して処理します。出力がモデルの上限に収まらない場合は議題ごとに分けてください。各工程を増やすと時間とモデル利用量も増えます。この版の品質・速度が別のアプリと同等とは未確認です。

## 同じ入力で品質と速度を比べる

`examples/benchmark-transcript.txt` は、予算の変更、取り消した日程、担当・期限、最後に決まったことを含む架空会議です。確認済み事項・書式・開始条件をそろえて両方のアプリで生成し、標準書式の本文と未決事項を `minutes.txt`・`open-issues.txt` としてそれぞれのフォルダに置きます。

```sh
python3 -m tools.benchmark_minutes --baseline ../minutes-baseline --candidate ../minutes-candidate --baseline-seconds 15
```

時間を別に測った場合は正しい実測値を指定します。固定の合成会議の採点は目安です。同等判定には、短い会議と長い会議を含む複数の入力で、重要な事実・更新で取り消された事項・未決の切り分けと、中央値・遅い回の所要時間を比較します。書式の一致だけでは同等とみなしません。

同じ事実を複数の処理区間に離して配置した架空の長文は、`python3 -m tools.benchmark_minutes --write-long-fixture ../benchmark-long.txt` で作れます。両版で入力とモデルをそろえ、テキストの読み込みから共有文の保存・読み戻しまでを測ります。短文と長文は別々に実測し、`--baseline-run` と `--candidate-run` に各回の出力先を渡してください。

`--require-parity` は、各版5回以上、入力のハッシュと生成先・モデル名の一致、候補版の必須項目すべての合格、所要時間の中央値が従来比1.20倍以内、p95が1.35倍以内を検査します。このモードでは `--candidate` と、指定する場合の `--baseline` はそれぞれ反復に含め、手入力の秒数は使いません。時間記録だけでは実行の証明にはならないため、実際に生成された内容の読みやすさや、想定外の誤記は別に確認します。

テスト: `python3 -B -m unittest discover -s tests` と `node --test tests/recorder.test.cjs`。画面のあるMacで会議タブ・履歴・コピーの検査も行う場合は、Pythonコマンドの先頭に `MINUTES_GUI_TEST=1` を付けます。Node.jsは録音の合成検査にだけ必要です。Mac用補助ツールはビルド後に `--self-test` を指定すると、実機の収音を始めずに検査できます。

公開用に変更を準備する場合は、対象ファイルだけを個別にステージし、`python3 -B tools/check_public_tree.py` を実行してください。これはファイルの許可リストと既知パターンの簡易検査です。会議データや設定の混入がないか差分も必ず確認してください。生成した資料は、公開用のソースフォルダとは別の場所に保存してください。

## ライセンス

[MIT License](LICENSE)。任意で利用するモデルや外部ツールは配布物に含めていません。
