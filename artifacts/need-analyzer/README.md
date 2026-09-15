# VibApp Need Analyzer

This experimental product-layer adapter asks the configured OpenAI-like model to turn
a short user request into a bounded NeedSpec proposal. It is an untrusted
proposer: the desktop client validates its enums and capability names, and the
deterministic NeedSpec completion path remains authoritative.

The model JSON boundary is exact: only the documented short keys or their normalized
long names are accepted. Any extra key (including `code`, `source`, or `shell`) and
any short/long alias collision fails closed before normalization; unknown capability
values are separately clipped inside the declared `capabilities` field.

Run a local synthetic check:

```sh
printf '%s' '{"title":"浇水记录","description":"做一个离线家庭盆栽浇水记录工具，按房间分类，关闭重开仍保留记录，不允许联网。"}' \
  | python3 need_analyzer.py analyze
```

Desktop supplies a bounded `model_config` over stdin and supports streaming Chat
Completions or Responses. A bearer key is optional and is never printed. The legacy
standalone environment configuration is documented in
`../product-config/llm.env.example`.
