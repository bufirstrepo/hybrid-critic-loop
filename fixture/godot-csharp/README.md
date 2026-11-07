# Demo fixture — Godot C# samplers

The filmed-demo fixture from the Hybrid Critic Loop design: two
engine-free C# classes of the kind a Godot 4 game pushes once per
physics tick, shipped as stubs that throw `NotImplementedException`.

- `Samplers/` — `SpeedSampler.cs`, `SteeringSampler.cs` (the contract targets)
- `SamplerTests/` — assertion harness: `dotnet run --project SamplerTests -- speed|steering`, exit code decides
- `AllocBench/` — measured-gate sensor: `dotnet run --project AllocBench -- SpeedSampler`, prints `{"alloc_bytes_per_tick": N}`

The classes carry no Godot types, so they compile and test with the
plain .NET 8 SDK and drop into a Godot 4 C# project unchanged.

**Status in the build environment: NOT RUN** — the machine that built
this repository has no .NET SDK installed, so these projects were
written and reviewed but never compiled there. They are the demo
fixture for a machine with Godot/.NET (the operator's), where the
contracts in `contracts/demo1-csharp.json` and
`contracts/demo2-csharp.json` run against them. The Python mirror
fixture (`fixture/python-sampler/`) implements the identical contract
shape and is the one exercised end-to-end in the build workspace.
