Generated from the Apache-2.0 component at commit
`2b920d638545723fb267bf749437d7f0cc5b5f1b` already vendored in
`scripts/research/proto/yellowstone/` (see its README and LICENSE_APACHE2).

Built with grpcio-tools 1.71.0 / protobuf 5.29.6; only the storage import
is made package-relative. No compiler, download or sys.path mutation at runtime.

Regenerate with the protoc command below, then make that import relative:

```bash
python -m grpc_tools.protoc -I scripts/research/proto/yellowstone \
  --python_out=sniper/feeds/yellowstone \
  scripts/research/proto/yellowstone/geyser.proto \
  scripts/research/proto/yellowstone/solana-storage.proto
```
