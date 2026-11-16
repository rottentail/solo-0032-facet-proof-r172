# Facet Proof

Facet Proof 是纯 Python 标准库实现的技术插图渲染后端：接收三角网格、材质及场景布置，冻结资源快照，渲染颜色、深度和物体编号图版，支持图版比对、校验、审阅和归档。使用 CPU 光栅化，不需要 GPU。

需要 macOS 或 Linux，以及 Python 3.11 或更高版本。启动：

```sh
python3 -m facetproof --root ./facet-data --host 127.0.0.1 --port 8796
```

默认绑定本机；服务没有账户或认证机制，只适合受信任的本地环境。持久化根目录包含 SQLite 数据库和内容寻址的图版文件。`POST /facet/v1/worker/step` 同步领取并执行一个待处理任务；`POST /facet/v1/worker/recover` 回收过期租约。进程启动时也会回收已过期租约。

可运行完整生产演示，输出三张 PNG：

```sh
python3 -m facetproof.demo --flow render-plates --output ./preview
```

演示使用临时数据库，不影响服务数据。其他流程名见 `facetproof/demo.py` 的 `FLOWS`（含 `lens-calibration`、`valid-framing`、`mapping-rejection`、`mapping-preview` 四个标定流程）。输入按顺序为：创建 `/facet/v1/meshes` 和 `/pigments`，创建 `/assemblies`，以版本号更新布置，再调用 `/assemblies/{id}/seal`；对 `/seals/{id}/exposures` 提交带唯一 `key` 的请求，然后执行 worker。图版的 `/passes/color.png`、`depth.png`、`object.png` 可供查看；`/pick?x=...&y=...` 返回物体及摄像机前向距离，并以 `valid_source` 标明该像素是否有唯一成像来源。`/verify` 校验文件，`/bundle` 导出包含快照及全部通道的 ZIP。

坐标使用右手空间，镜头支持透视和正交投影，旋转以度为单位；颜色输入为 0–1 sRGB，内部以线性光计算。每个图版包含 RGB、32 位浮点深度、32 位无符号物体编号和对应 PNG；背景深度为正无穷。图版保留颜色与深度的输入快照和摘要。服务有图像尺寸、采样、面数和渲染时间上限。

## 镜头标定：偏移主点与径向畸变

镜头可声明 `principal_point: [px, py]`（等效半高为 1 的归一化单位，±4 内）、`distortion: {"k1","k2","k3"}`（径向多项式，k1 ∈ ±4，k2/k3 ∈ ±8）和 `framing: "full" | "valid"`，用于把零件图与实拍照片对齐。未声明或全零的镜头保持原有对称透视/正交行为，输出字节不变。

畸变映射 `rd(r) = r·(1 + k1 r² + k2 r⁴ + k3 r⁶)` 只在单调递增且比例为正时单射。服务从冻结镜头确定性地求出**有效成像范围**（以主点为圆心的圆盘半径），并在图版 `mapping` 报告中公布：圆心、半径、取景矩形、实际容差。圆盘之外的像素没有唯一来源方向，一律写入保留身份 `0xFFFFFFFF`、正无穷深度和保留填充色（品红），不计入 `covered_pixels`——填补边缘不会被伪称为真实表面。`GET /plates/{id}/pick` 对这类像素返回 `valid_source: false` 和 `reason: "outside_valid_imaging_range"`，不会凭求解偶然落点报出零件。

取景范围由用户明确选择：`framing: "full"` 保留请求的全部画布，无效边缘按上法标记；`framing: "valid"` 把图版裁到有效圆盘的包围盒（不小于 16×16），牺牲圆盘外的零件换取全部像素有效。有效画质由曝光请求的 `mapping_tolerance`（0.1–8，最终像素单位，默认 0.5）声明：它限制三角剖分逼近曲线畸变的最大误差。取景选择随镜头冻结进快照，画质随请求冻结进曝光，两者都进入图版 `mapping` 报告与分块指纹，重渲只能复用完全相同的定义。

有效范围为空（例如强畸变加偏心使圆盘落在画布外，或不足 16 像素）的曝光在提交时以 `valid_range` 拒绝并说明原因。局部难解映射不会让工作量失去上界：剖分深度封顶、计划三角形总数封顶 240000，超出时曝光以 `mapping_budget` 失败并提示提高容差或缩小画布，不会偷偷缩小有效范围或减弱畸变。每次尝试仍有 45 秒渲染预算。

`POST /facet/v1/assemblies/{id}/preview` 在编辑标定时同步预览当前草稿（宽/高 ≤ 192，单采样，15 秒预算）：返回 base64 颜色图、`mapping` 报告和实际采用的 `mapping_tolerance` 与三角形数。预览不写曝光、图版或额度，响应标记 `approximate: true`；正式曝光各自记录并冻结自己的画质，二者可由报告区分。

标定镜头的图版使用 `facet-plate-2` 格式并携带 `mapping` 报告；传统镜头仍是 `facet-plate-1`。图版比较要求两侧 `mapping` 完全一致（焦距、畸变、取景与画质均相同），否则以 `alignment` 拒绝，并额外报告 `invalid_changed`。独立读取器核验 `facet-plate-2` 时会从冻结镜头与请求重新推导有效半径、圆心和取景矩形，与图版声明不符即拒绝。

曝光现在按高采样画布的 64×64 块保存计算进度。`GET /facet/v1/exposures/{id}/progress` 返回已经提交的块和像素数。块完成不等于图版成功：只有所有块完整、内容可读、租约仍有效且未取消，才会生成最终图版。进程中断后，先恢复过期租约，再执行工作器即可复用同一冻结输入的已提交块；显式重试失败曝光也复用完整块。渲染仍有每次尝试 45 秒预算，服务不会自动启动后台工作器。

`GET /facet/v1/resources` 查询画布额度，`PUT` 同一路径以 `capacity` 和 `revision` 修改策略。额度单位为高采样像素，即 `width × height × samples²`，不是进程物理内存上限。接收新曝光与预留额度属于同一事务；同键重发不重复预留。运行中的取消须等工作器确认终态才归还额度，失租接管继续持有原曝光的额度。缩小策略不撤销已经接受的承诺，响应会显示超出新策略的部分。首次打开旧目录时会补记已有排队及运行任务的承诺。`POST /facet/v1/resources/reconcile` 核对持久状态，无法补足的条目保留等待事实，不伪造成功或取消。

交付接收者可以脱离服务核验 ZIP：

```sh
python3 -m facetreader ./plate-bundle.zip --output ./received-plate
```

独立读取器不导入渲染服务代码，自行检查 ZIP、PNG、原始通道和冻结来源；有效时退出 0，拒绝时退出 2。它限制输入和解压体积，只接受当前两份元数据及六个通道，核验 `facet-plate-1` 与 `facet-plate-2`；对标定图版会重新推导镜头映射并与声明比对。核验通过表示包内事实一致，不能证明外部来源真实，也不能证明服务中当前审阅意见仍有效。输出目录提供颜色图、来源报告和拾取样例。

完整在线备份分为固定快照和续做复制两步：`POST /facet/v1/maintenance/backups` 返回操作标识；`POST /facet/v1/maintenance/backups/{id}/resume` 复制并核对该快照引用的最终通道及在途分块。可以在中断后对同一操作续做，不会重新抓取另一个 SQL 时点。`GET /facet/v1/maintenance/backups/{id}` 查询状态，末尾追加 `/verify` 独立复核完成后的备份。缺失或损坏的来源内容会明确失败。备份位于数据根的 `backups/{id}/data`；恢复演练应先复制这份目录到新的数据根，再启动服务，避免改写备份本身。SQL 中的维护控制行属于备份时的历史记录，不能在另一数据根继续原机器的操作。

`GET /facet/v1/maintenance/inspect` 提供固定 SQL 观察边界下的通道引用、共享关系及字节核验。无引用内容与可能仍在写入的内容须区分，巡检不提供删除操作。图版逻辑清退继续保留内容；本服务没有物理垃圾回收功能。

当前基线不包含测试套件，自动化测试有意延期；`facetproof.demo` 提供有界的业务流程检查，不应当作完整的测试覆盖。
