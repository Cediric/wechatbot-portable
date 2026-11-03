# 第三方运行组件

交付中保留原始发行文件、许可证及来源。`vendor/vendor-manifest.json` 包含逐文件SHA-256、大小、依赖名称/版本及轮子元数据中的许可证声明，`vendor/licenses/` 另存发行文件内的许可证和NOTICE。元数据仅作为来源记录，以各组件实际许可证为准。

| 组件 | 版本/发行 | 来源及许可 |
|---|---|---|
| uv | 0.13.0 Windows x64 | Astral官方GitHub发行；MIT / Apache-2.0，两份许可证均保留 |
| CPython | 3.12.12 + python-build-standalone 20260211 | Astral官方GitHub发行；PSF及发行中所列组件许可证随原ZIP和license目录保留 |
| AstrBot | 4.28.2 | 官方PyPI纯Python发行，AGPL-3.0；原轮子含源码和许可证，完整上游源码为 [v4.28.2](https://github.com/AstrBotDevs/AstrBot/tree/v4.28.2) |
| FFmpeg / ffprobe | 9.0.2 Gyan essentials Windows构建 | [FFmpeg官方页面所列Windows构建](https://ffmpeg.org/download.html)；Gyan归档的GPL许可证及构建README保留，参见 [Gyan构建页](https://www.gyan.dev/ffmpeg/builds/) 和 [FFmpeg许可](https://ffmpeg.org/legal.html) |
| Python依赖 | 159个锁定轮子 | 原PyPI轮子及许可证；详见vendor-manifest中的dependencies和license_files |

两个从经过哈希校验的PyPI源代码包构建的纯Python轮子（aiocqhttp/jieba）有单独构建来源与产物哈希，见 `vendor/built-wheels-provenance.json`。这些构建未更改项目源代码。

Microsoft Visual C++和VB-Cable安装程序不在本包中分发；仅在用户端从其官方站点下载并核验，使用和安装遵循各自许可证与Windows UAC。下载地址/固定哈希记录于runtime-manifest。

再次分发本包时请完整保留上游许可证、NOTICE、构建来源和源代码访问资料，并遵守相应开源组件的源码提供要求。
