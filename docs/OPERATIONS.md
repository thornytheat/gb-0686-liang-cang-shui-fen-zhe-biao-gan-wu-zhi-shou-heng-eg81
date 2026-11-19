# 运行维护

主服务端口与Compose保持一致。数据保存在records命名卷，应用代码不需要写入权限。

```sh
docker compose logs --tail=80 app
docker compose restart app
docker compose exec -T app pytest -q
docker compose down --remove-orphans
```

健康检查GET /health。失败输入返回原因码，结构性错误422，过大文件413，陈旧复核409。
文件内容最大4MB。批次先计算再原子保存，失败不留半条记录。证据CSV按原始行号定位。

## 仓容与入账

仓账存在同一 SQLite 的 `bins`、`bin_entries`、`alloc_commits` 表。容量口径是干物质 kg，通过 `bin_capacity` 参数给出（JSON 或 `仓1=12000,仓2=11000`）。
提交仓账是单事务：先逐仓核对 `opening_kg` 与当前账面，再累加本批；任一仓超容或账面已被改动则整批回滚（409/`CAPACITY`），旧版库容原样保留。
同一批磅单以输入 SHA256 识别。改仓容后重跑同一批，会扣回该批旧占用再按新结果入账，不会二次占容；同一批次重复提交直接复用旧结果。
暂缓车不写 `bin_entries`、不占容量，只出现在分配页与 `allocation.csv`；证据 CSV 将其标为 `CAPACITY`。
需要重算全部库容时，停止服务并确认不再需要记录后删除 records 卷，再启动重建。
