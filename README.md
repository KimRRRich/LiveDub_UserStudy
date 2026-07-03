# User Study 视频评分系统

这是一个轻量自托管方案：FastAPI + SQLite + 原生前端。适合 512x512、单个约 3MB 的视频评分实验。

## 功能

- 用户输入用户名和密码
- 新用户名会自动注册，并使用首次输入的密码作为登录密码
- 已注册用户名必须密码正确才能继续评分；密码错误会提示用户名已注册或密码错误
- 每个用户的样本顺序随机，并保存在服务端
- 同一样本下的 4-5 个方法视频放在同一个页面中对比
- 播放其中一个视频时，其他视频会自动暂停，避免声音叠加
- 五个维度 1-5 分：画面质量、遮挡处理、唇形同步、牙齿质量、身份一致性
- 每个样本可显示一张原始人物参考图，用于判断配音后的身份一致性
- 每条方法视频都会单独保存四个维度评分到 SQLite
- 样本导航支持三态：已完成绿色、部分完成橙色、未开始白色
- 已开始的样本必须完整评分；未开始样本可跳过并提前提交
- 刷新后可用用户名和密码继续评分
- 已完成用户可重新登录继续修改评分
- 完成后提交
- 管理员登录后台查看注册人数、完成进度、方法/视频平均分
- 管理员后台一键导出已有评分 CSV
- 管理员后台可删除单个用户、清空单个用户评分、清空所有用户和评分
- 前端不显示方法名，但数据库和 CSV 会记录 method

## 本地运行

```bash
cd /home/kim/work/LiveDub_UserStudy
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python tools/build_manifest.py --video-dir videos --output videos.json
ADMIN_TOKEN=my-token ADMIN_USERNAME=admin ADMIN_PASSWORD=123456 uvicorn app:app --host 0.0.0.0 --port 8000
```

打开：

```text
http://服务器IP:8000
```

健康检查：

```text
http://服务器IP:8000/api/health
```

导出 CSV：

```text
http://服务器IP:8000/api/export.csv?token=your-secret-token
```

管理员后台：

```text
http://服务器IP:8000
```

使用 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 登录。如果没有设置 `ADMIN_PASSWORD`，系统会使用 `ADMIN_TOKEN` 作为管理员密码。

默认管理员用户名是：

```text
admin
```

默认管理员密码规则：

```text
优先使用 ADMIN_PASSWORD；如果没有设置 ADMIN_PASSWORD，则使用 ADMIN_TOKEN。
```

## 临时后台运行

如果只是临时在公网服务器上启动服务，不想因为 SSH 终端关闭导致页面无法访问，可以用 `nohup` 后台运行：

```bash
cd /home/kim/work/LiveDub_UserStudy
nohup .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000 > userstudy.log 2>&1 &
```

查看进程是否还在：

```bash
ps -ef | grep 'uvicorn app:app' | grep -v grep
```

查看日志：

```bash
tail -f /home/kim/work/LiveDub_UserStudy/userstudy.log
```

停止服务：

```bash
pkill -f 'uvicorn app:app'
```

访问地址：

```text
http://服务器公网IP:8000
```

注意：服务器安全组和本机防火墙都需要放行 `8000/tcp`。`nohup` 适合临时实验；长期部署建议使用下面的 `systemd` + `nginx` 流程。

## 视频命名

推荐把视频按样本目录放到 `videos/`：

```text
videos/<audio_id>/<method>.mp4
```

例如：

```text
videos/001/gt.mp4
videos/001/methodA.mp4
videos/001/methodB.mp4
videos/002/gt.mp4
videos/002/methodA.mp4
```

然后生成清单：

```bash
python tools/build_manifest.py --video-dir videos --output videos.json
```

生成的 `videos.json` 类似：

```json
[
  {
    "id": "001_methodA",
    "audio_id": "001",
    "method": "methodA",
    "url": "/videos/001/methodA.mp4"
  }
]
```

也兼容旧式扁平命名：

```text
videos/001_methodA.mp4
videos/001_methodB.mp4
```

如果你的文件名不方便按这些规则命名，也可以手动编辑 `videos.json`。

## 服务器需要安装什么

最低建议：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx
```

可选但推荐：

```bash
sudo apt install -y certbot python3-certbot-nginx
```

不需要安装 Node.js、MySQL、PostgreSQL。SQLite 会自动创建在 `data/study.db`。

## 公网部署流程

假设部署目录为 `/opt/userstudy`。

1. 上传项目代码和视频：

```bash
sudo mkdir -p /opt/userstudy
sudo rsync -av --exclude ".venv" /home/kim/work/LiveDub_UserStudy/ /opt/userstudy/
```

2. 安装 Python 依赖：

```bash
cd /opt/userstudy
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
```

3. 放入视频并生成 `videos.json`：

```bash
sudo mkdir -p /opt/userstudy/videos
sudo .venv/bin/python tools/build_manifest.py --video-dir videos --output videos.json
```

4. 设置权限：

```bash
sudo mkdir -p /opt/userstudy/data
sudo chown -R www-data:www-data /opt/userstudy
```

5. 配置 systemd：

```bash
sudo cp /opt/userstudy/deploy/userstudy.service /etc/systemd/system/userstudy.service
sudo nano /etc/systemd/system/userstudy.service
```

把 `ADMIN_TOKEN=change-this-token` 改成你自己的随机字符串，并设置：

```text
ADMIN_USERNAME=admin
ADMIN_PASSWORD=你的管理员密码
```

启动服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now userstudy
sudo systemctl status userstudy
```

6. 配置 nginx：

```bash
sudo cp /opt/userstudy/deploy/nginx-userstudy.conf /etc/nginx/sites-available/userstudy
sudo nano /etc/nginx/sites-available/userstudy
```

把 `server_name your-domain.com;` 改成你的域名或服务器 IP。

启用配置：

```bash
sudo ln -s /etc/nginx/sites-available/userstudy /etc/nginx/sites-enabled/userstudy
sudo nginx -t
sudo systemctl reload nginx
```

7. 如果有域名，开启 HTTPS：

```bash
sudo certbot --nginx -d your-domain.com
```

## 运维命令

查看服务日志：

```bash
sudo journalctl -u userstudy -f
```

重启服务：

```bash
sudo systemctl restart userstudy
```

备份数据：

```bash
sudo cp /opt/userstudy/data/study.db /opt/userstudy/data/study.db.backup
```

## 注意事项

- 正式实验前，先用 1-2 个测试用户名完整走一遍并导出 CSV。
- `ADMIN_TOKEN` 不要泄露，CSV 导出接口依赖它。
- 如果修改了视频集合，正在评分的用户原有随机顺序不会自动改变。建议正式开始后不要再改 `videos.json`。
- 如果需要重开实验，备份后删除 `data/study.db`，再重启服务。
