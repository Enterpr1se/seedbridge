# Seedbridge

Filmix 經 CloudDrive 改成 TMDB / Emby 資料夾同檔名之後，自動通知 qBittorrent 更新路徑，種子繼續做種。程式會自己暫停該種子幾秒再恢復，你唔使手動暫停，亦唔使人手改 qBittorrent 的檔名。

映像檔係 linux/amd64，對應 TS-464。

## Container Station 用 URL 匯入

永遠指向最新版（之後更新都係同一條）：

```
https://github.com/Enterpr1se/seedbridge/releases/latest/download/seedbridge.tar.gz
```

目前這個版本：

```
https://github.com/Enterpr1se/seedbridge/releases/download/v1.0.0/seedbridge.tar.gz
```

1. Container Station → **映像檔** → **匯入**。
2. 如果匯入視窗可以貼 URL，貼上面第一條。
3. 如果只可以選檔案，在 NAS 用 File Station 的下載，或者 SSH 先存到共享資料夾：

```bash
wget -O /share/Public/seedbridge.tar.gz \
  https://github.com/Enterpr1se/seedbridge/releases/latest/download/seedbridge.tar.gz
```

然後匯入時選 **本機 QNAP 裝置**，揀 `seedbridge.tar.gz`。匯入後映像檔名稱係 `seedbridge:latest`。

Repo 要保持 **Public**。私人 repo 的下載網址要登入，Container Station 拎唔到。

## 建立容器

用匯入咗的映像檔建立容器：

| 項目 | 值 |
|---|---|
| 名稱 | `seedbridge` |
| 重新啟動 | 除非手動停止 |
| 網路 | 一個未用的獨立 IP（macvlan）。如果用橋接，對外埠填 `8080` |
| 目錄 `/data` | 一個空目錄，例如 `/share/Container/seedbridge/data`。登入、設定、改名紀錄都在這裡，更新時一定要掛返同一個 |
| 目錄 `/media` | TS-464 的影片共享資料夾，必須同 qBittorrent 係同一個資料夾 |

瀏覽器開 `http://容器IP:8080`，第一次會叫你建立管理員帳號。

## 設定

設定頁：

- **qBittorrent**：`192.168.8.197`、WebUI 埠、帳號密碼，然後「測試連線」。
- **CloudDrive**：容器 IP 同埠（預設 19798）。只係用來測試呢部容器睇唔睇到 CloudDrive。改名通知係 CloudDrive 打過來，不是 Seedbridge 連去 CloudDrive 改檔。
- **路徑對應**三欄，指同一樣位置的三種寫法：

| CloudDrive webhook 入面的路徑 | Seedbridge 容器內 | qBittorrent 容器內 |
|---|---|---|
| webhook 的 `source_file` 前綴 | 掛住影片的路徑，預設 `/media` | qBittorrent 儲存路徑，常見 `/downloads` |

例子：Filmix 把 `/115/Video/Movie/Old.mkv` 改成 `/115/Video/Movie/New.mkv`，而這個目錄在 NAS 上係 `/share/Video`，qBittorrent 入面係 `/downloads`：

| CloudDrive | 容器內 | qBittorrent |
|---|---|---|
| `/115/Video` | `/media` | `/downloads` |

三欄的剩餘部分必須一樣。先按「檢查容器內目錄」，確認 `/media` 睇到影片。

建議第一次：

1. 勾「接收 webhook 並處理」。
2. 勾「只記錄，不改 qBittorrent」。
3. 等 Filmix 改一次名，去「改名紀錄」睇舊路徑同新路徑對唔對。
4. 取消「只記錄」，按總覽的「開始自動改名」。

你自己暫停住的種子唔會被打開。失敗時檔案會留返 Filmix 的新名，不會刪檔，亦不會 Force Recheck。

qBittorrent 5.3 的 renameFile／renameFolder 可以回 200，但種子路徑不變，除非新路徑已經在磁碟上。程式會先暫停種子，只在該種子的儲存目錄裡面搬內容（不會搬 Movies、西片、download 這些上一層），再呼叫 rename API 同步路徑，然後恢復做種。Filmix 已經放到最終路徑的檔案不會被拖回中途名。資料夾同檔案路徑一定會同步。種子標題（顯示名稱）預設不改，方便分辨同一套片的不同來源。設定頁勾「同時更改種子標題（顯示名稱）」之後，標題才會改成 Filmix 的資料夾名。qBittorrent 選擇不下載、磁碟上沒有的檔會跳過，不會拖垮同一種子裡已經下載的影片和字幕。

## 重試失敗的改名

改名紀錄入面，失敗列如果有「重試」，直接按。唔使把檔案改回舊名，亦唔使再等兩分鐘。

重試用失敗當下保存的舊路徑同新路徑，再做一次分階段改名。更新到 v1.0.2 之前的失敗列沒有這份路徑，不能重試，要再改一次名先會出現新的失敗列。

## CloudDrive webhook

總覽頁有一條完整 URL（已經帶 token）。CloudDrive 的 `webhook.toml` 用總覽頁那條，或者照 `webhook.toml.example`，把 IP 同 token 換成你的。

只需要 `rename`。Emby 直接讀同一個資料夾的話，唔使再轉去 Symedia。

## 之後點更新

唔使再把檔案經電腦傳上 NAS。兩種方法，設定都留在 `/data`。

**拉取（較省事）：** Container Station → 映像檔 → 拉取，名稱填：

```
ghcr.io/enterpr1se/seedbridge:latest
```

公開映像檔，唔使登入。如果拉唔到，在登錄檔加入 `https://ghcr.io`，帳號留空。之後有新版，再拉取同一個名稱，停掉舊容器，用新映像檔建立，掛返同一個 `/data` 同 `/media`。

**匯入檔：** 用上面同一條 latest URL 再下載，再匯入。映像檔名稱係 `seedbridge:latest`。如果 Container Station 話格式不支援，改匯入這個傳統格式：

```
https://github.com/Enterpr1se/seedbridge/releases/download/v1.0.0/seedbridge-1.0.0.tar.gz
```

兩個做法都一樣：停掉舊容器，唔好刪 `/data`，新容器掛返同一個 `/data` 同 `/media`，IP 用返同一個。打開網頁，帳號同設定會在。

## 原始碼

```
app.py          WebUI、登入、webhook
engine.py       對齊 qBittorrent 的改名
db.py           設定同紀錄（SQLite，放在 /data）
static/         介面
Dockerfile      自己 build 用
```

容器入面沒有密碼。qBittorrent 密碼只寫入 `/data/seedbridge.db`。

自己 build：

```bash
docker build -t seedbridge:latest .
```

## 程式實際做咩

CloudDrive 送來 rename 之後，程式會等幾秒（預設 8 秒，等 Filmix 一次改完資料夾同檔案），然後：

1. 用舊路徑搵到對應的種子。
2. 暫停該種子（你原本已經暫停的就保持暫停）。
3. 用 qBittorrent API 更新資料夾名同檔名，令做種路徑等於磁碟上的新名字。
4. 恢復做種。

對唔到、路徑衝突、或者 API 失敗，會寫入改名紀錄，唔會刪檔。
