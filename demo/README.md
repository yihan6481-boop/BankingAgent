# 初始化数据

`spec/demo_data_design_v1.json` 是 `tools/final_pg_demo.py` 读取的合成数据规格，用于生成演示用户、账户、联系人、产品及流水。它不是测试集，不是当前服务器数据，也不携带私密登录密码。

`tools/final_pg.py` 保留 init、seed、validate、ready 和显式重置管理命令，已移除测试运行命令。init 内部仍校验导入结果，但不再写入历史验收目录。普通启动不会自动初始化或重置现有数据库。

这些工具依赖本机 PostgreSQL、受限角色和工程外 passfile。仅对明确准备好的专用演示环境使用初始化命令；完整空环境部署尚未验证。
