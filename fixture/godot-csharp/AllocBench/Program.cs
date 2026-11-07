using System.Text.Json;
using Samplers;

// Measured-gate sensor: managed bytes allocated per tick.
//   dotnet run --project AllocBench -- SpeedSampler|SteeringSampler
// Warms up, then measures GC.GetAllocatedBytesForCurrentThread() delta
// over a fixed tick loop (push + average per tick). Prints one JSON
// object: {"alloc_bytes_per_tick": <number>}.
// A ring-buffer implementation allocates 0 bytes per push; a
// list/LINQ implementation allocates on every tick. Deterministic.
string which = args.Length > 0 ? args[0] : "SpeedSampler";
const int Warmup = 2000;
const int Ticks = 20000;

Action<float> push;
Func<float> average;
if (which == "SpeedSampler")
{
    var s = new SpeedSampler();
    push = s.Push;
    average = s.Average;
}
else if (which == "SteeringSampler")
{
    var s = new SteeringSampler();
    push = s.Push;
    average = s.Average;
}
else
{
    Console.Error.WriteLine($"unknown sampler '{which}'");
    return 2;
}

for (int i = 0; i < Warmup; i++)
{
    push(i * 0.5f);
    _ = average();
}
GC.Collect();
GC.WaitForPendingFinalizers();
long before = GC.GetAllocatedBytesForCurrentThread();
for (int i = 0; i < Ticks; i++)
{
    push(i * 0.25f);
    _ = average();
}
long after = GC.GetAllocatedBytesForCurrentThread();
double perTick = (double)(after - before) / Ticks;
Console.WriteLine(JsonSerializer.Serialize(new { alloc_bytes_per_tick = perTick }));
return 0;
