# 实验记录异常复核台

用于教研人员查看本地记录、选择业务范围、核对原始数据并保存复核材料。当前页面展示逐字段范围与中位偏离，描述值不代替统计推断或业务决定。所有示例都是本地生成的虚构记录，不含个人信息、真实研究数据或受限材料。

## 运行

需要 Python 3.11，运行不依赖第三方服务或第三方包。推荐使用锁定基础镜像的容器；也可以在项目根目录直接启动。

```bash
python3 -m measurement_review --host 127.0.0.1 --port 8080
```

浏览器打开 `http://127.0.0.1:8080`。状态默认保存在 `.state/research.sqlite3`，可用 `--state` 指定独立目录。页面提供范围选择、表格和图、原始记录查看、完整数据导入、保存和导出。保存的是数据版本与当时的描述，不会因后续导入变化。

```bash
docker build -t measurement_review:local .
docker run --rm -p 8080:8080 -v measurement_review-state:/app/.state measurement_review:local
```

## 检查

```bash
python3 -m unittest discover -s tests -v
```

回归覆盖数据口径、HTTP 交互、非法导入回滚、不可变版本与重启后的历史读取。测试使用临时状态目录，不改变演示数据。详见 `docs/data-contract.md`、`docs/http.md` 和 `docs/walkthrough.md`。如果扩展运行依赖，请同时在依赖清单和镜像中固定版本。
