using System;

namespace Samplers;

/// <summary>
/// Rolling average of vehicle speed, pushed once per physics tick.
/// Fixed capacity of 120 samples; Average() returns 0 when empty.
/// Stub: both methods throw until a candidate implements them.
/// Engine-free on purpose: the class compiles and tests outside Godot,
/// and drops into a Godot 4 C# project unchanged.
/// </summary>
public sealed class SpeedSampler
{
    public const int DefaultCapacity = 120;

    private readonly int _capacity;

    public SpeedSampler(int capacity = DefaultCapacity)
    {
        _capacity = capacity;
    }

    public int Capacity => _capacity;

    public void Push(float mps) => throw new NotImplementedException();

    public float Average() => throw new NotImplementedException();
}
