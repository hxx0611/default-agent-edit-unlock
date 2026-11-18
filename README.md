# default-agent-edit-unlock

QwenPaw 控制台里 default 智能体的编辑按钮是灰的，点不了。这个插件让它能点。

> Unlocks the default agent's edit buttons in the QwenPaw console.

有意思的是后端根本不拦——`PUT /api/agents/default` 随便调。纯粹是前端 `AgentGallery` 那个 bundle 把两处编辑按钮的 `disabled` 写死成了 `id === "default"`。所以这个插件就解开了那两处，别的不动。

## 原理

每次 app 启动时：

1. 找到 `console/assets/AgentGallery-*.js`，改写两处编辑按钮的 disabled 表达式（卡片快捷编辑 + 详情抽屉编辑）。
2. 给改过的文件做缓存爆破：引用处追加内容哈希 query，再往 index.html 注入一个 import map。这步不能省——静态资源没有 Cache-Control，不爆缓存的话浏览器会一直用缓存里的原版，补丁等于白打。亲测踩出来的坑。
3. 原文件备份到插件目录 `backups/`，卸载时逐字节还原。

置顶、拖拽、启停、删除的锁定原样保留。后端本来也不让对 default 做这些操作，前后端保持一致。

## 安装

整个目录拷进 QwenPaw 的插件目录（`<工作目录>/plugins/`），重启 app。不用配置，目录扫描发现即启用。

插件目录记得放持久化存储上。补丁改的是 venv 里的静态文件，容器重建、pip 升级都会把它抹掉——启动钩子每次启动会重打，插件本身还在就能自愈。

## 关闭 / 卸载

- 临时关：环境变量 `QWENPAW_DEFAULT_EDIT_UNLOCK=0`，启动时自动还原原版。
- 卸载：正常卸载就行，uninstall hook 会还原所有改过的文件，包括 index.html。

## 兼容性

在 qwenpaw 2.3.0 上实测。补丁锚定 `common.edit` 这个 i18n key 和周围代码结构，前端小改版一般还能命中；大改版命不中时会打 warning 然后跳过，不会乱改文件。

编辑保存会触发 default agent reload，正常现象。