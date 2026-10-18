# 统计接入与兼容性

核对日期：2026-10-18。官方源码版本 4.29.1，提交 `dffe922773725e9c178392c423676ad65044748c`。

公共 [`on_llm_response` 文档](https://docs.astrbot.app/dev/star/guides/listen-message-event.html) 不能保证涵盖直接 Provider 请求与工具循环中每一轮响应。
本插件使用小范围可恢复的运行时包装，不编辑 AstrBot 或其他插件的磁盘文件。

1. [`PipelineScheduler.execute`](https://github.com/AstrBotDevs/AstrBot/blob/dffe922773725e9c178392c423676ad65044748c/astrbot/core/pipeline/scheduler.py)：在消息生命周期建立 ContextVar，将并发请求归属到原始用户；生命周期结束时让子任务继承的归属失效。
2. [`StarRequestSubStage` 模块的 `call_handler`](https://github.com/AstrBotDevs/AstrBot/blob/dffe922773725e9c178392c423676ad65044748c/astrbot/core/pipeline/process_stage/method/star_request.py)：在停止检查与插件检查之后统计实际调用的注册指令，不以唤醒阶段的候选列表代替执行次数。
3. [`CommandFilter`](https://github.com/AstrBotDevs/AstrBot/blob/dffe922773725e9c178392c423676ad65044748c/astrbot/core/star/filter/command.py)：使用 handler 元数据、解析成功标记与规范指令名，不重复执行过滤器，不从原文抽取参数。
4. [`Provider`](https://github.com/AstrBotDevs/AstrBot/blob/dffe922773725e9c178392c423676ad65044748c/astrbot/core/provider/provider.py)：包装配置中的具体 Provider 类的聊天方法，逐条消息入口发现新加载的 Provider。相同 Provider 的内部继承/super/stream fallback 按一次调用计，不同 Provider 的嵌套调用分别计。
5. [`TokenUsage / LLMResponse`](https://github.com/AstrBotDevs/AstrBot/blob/dffe922773725e9c178392c423676ad65044748c/astrbot/core/provider/entities.py)：`input_other + input_cached + output`；缺失为未知。
6. [`OpenAI Provider`](https://github.com/AstrBotDevs/AstrBot/blob/dffe922773725e9c178392c423676ad65044748c/astrbot/core/provider/sources/openai_source.py)：兼容端点缺少 usage 时可能产生全零占位 `TokenUsage()`，本插件在 raw usage 明确缺失时将其还原为未知。

初始化接入失败会提示统计不可用；Provider 方法被实例覆盖等无法观察的情况会提示统计不完整。观察层写入错误不会终止原指令或模型请求。
卸载只恢复仍由自己持有的包装；若另一个插件之后又包装了它，保留后者的链路，并使旧统计包装失效。
依赖内部接口意味着更新 AstrBot 后需要重新核对；不得声称全部外部模型流量都已统计。
