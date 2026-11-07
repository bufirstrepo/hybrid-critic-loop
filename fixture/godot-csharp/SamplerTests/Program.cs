using Samplers;

// Minimal assertion harness (no test-framework packages to restore):
//   dotnet run --project SamplerTests -- speed|steering
// Exit code 0 = all assertions passed, 1 = at least one failed.
string which = args.Length > 0 ? args[0] : "speed";
int failures = 0;

void Check(bool ok, string name)
{
    Console.WriteLine($"{(ok ? "PASS" : "FAIL")} {name}");
    if (!ok) failures++;
}

bool Near(float a, float b) => Math.Abs(a - b) < 1e-4f;

if (which == "speed")
{
    var s = new SpeedSampler();
    Check(Near(s.Average(), 0f), "empty average is zero");
    s.Push(10f);
    Check(Near(s.Average(), 10f), "single push");
    var s2 = new SpeedSampler();
    foreach (var v in new[] { 2f, 4f, 6f }) s2.Push(v);
    Check(Near(s2.Average(), 4f), "average of several");
    var s3 = new SpeedSampler();
    for (int i = 0; i < 130; i++) s3.Push(i);
    float expected = 0f;
    for (int i = 10; i < 130; i++) expected += i;
    Check(Near(s3.Average(), expected / 120f), "capacity wraparound");
    var s4 = new SpeedSampler(capacity: 3);
    foreach (var v in new[] { 1f, 2f, 3f, 4f }) s4.Push(v);
    Check(Near(s4.Average(), 3f), "custom capacity");
}
else if (which == "steering")
{
    var s = new SteeringSampler();
    Check(Near(s.Average(), 0f), "empty average is zero");
    s.Push(-15f);
    Check(Near(s.Average(), -15f), "single push");
    var s2 = new SteeringSampler();
    foreach (var v in new[] { -10f, 0f, 10f }) s2.Push(v);
    Check(Near(s2.Average(), 0f), "average of several");
    var s3 = new SteeringSampler();
    for (int i = 0; i < 130; i++) s3.Push(i);
    float expected = 0f;
    for (int i = 10; i < 130; i++) expected += i;
    Check(Near(s3.Average(), expected / 120f), "capacity wraparound");
    var s4 = new SteeringSampler(capacity: 3);
    foreach (var v in new[] { 1f, 2f, 3f, 4f }) s4.Push(v);
    Check(Near(s4.Average(), 3f), "custom capacity");
}
else
{
    Console.WriteLine($"unknown suite '{which}' (expected speed|steering)");
    return 2;
}

Console.WriteLine(failures == 0 ? "ALL PASS" : $"{failures} FAILURES");
return failures == 0 ? 0 : 1;
