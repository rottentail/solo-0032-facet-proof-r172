# 独立离线图版读取

在项目目录运行：

```sh
python3 -m facetreader plate-bundle.zip --output inspected
```

有效包在标准输出返回 JSON 并退出 `0`；格式或内容拒绝返回
`{"valid": false, "error": "稳定错误码", "message": "原因"}` 并退出 `2`。
`--output` 写入 `summary.json`、`provenance.json`、`picks.json` 和原颜色
`color.png`。输入 ZIP 从不按成员路径解压到磁盘。

Python API 只使用标准库，不导入生产渲染器：

```python
from facetreader import inspect_bundle, validate_bundle

report = inspect_bundle("plate-bundle.zip")
report = validate_bundle(raw_zip_bytes)
if report["valid"]:
    print(report["summary"], report["picks"])
```

两个 API 均返回可 JSON 序列化的字典。拾取中的 `depth` 是相机前向距离，
背景为 `null`；`object_index` 只在该图版内有效，来源由冻结部件键、网格及
材质标识和修订记录解释。

读取器验证 `facet-plate-1` / `facet-plate-2` 图版与 `facet-seal-1` 快照：
ZIP 恰有两个元数据成员和六个
通道；规范 JSON 内容摘要、镜头摘要、通道描述符、图版来源及对象映射相符；
PNG 的块 CRC、zlib 流和全部五种行滤波被独立解码。颜色 PNG 必须与原始
RGB 相同，对象预览须与局部编号一致，深度与背景/对象编号必须对应。
对 `facet-plate-2`，读取器还从冻结镜头与曝光请求独立重推有效成像半径、
主点圆心和取景矩形，逐项核对图版的 `mapping` 报告；有效范围之外的像素
必须携带保留身份 `0xFFFFFFFF`、正无穷深度和保留填充色，三者缺一即拒绝，
统计中的 `invalid_pixels` 必须与通道一致。
深度预览也被完整解码，但不按持久化后的 float32 重新断言其灰阶：生产者
在 float64 深度降为 float32 前生成预览，舍入边界可能产生灰阶差异。

支持非交错、无透明度的 8 位 RGB PNG；输入 ZIP 上限为 64 MiB，总解压
上限为 128 MiB，JSON 成员上限为 64 MiB，PNG 成员上限为 4 MiB，原始
通道按最大 512×512 尺寸限定。解码长度同时受图版尺寸约束。高度可压缩的
合法背景不会仅因压缩比高而被拒绝。

成功表示包内字节及来源声明相互一致。它不证明图像由可信服务生成：攻击者
可以同时替换通道、元数据和所有包内摘要。读取器不重新渲染冻结输入，
ZIP 也不包含审阅记录或服务当前隔离状态，因此不能据它宣称审阅仍然有效。
