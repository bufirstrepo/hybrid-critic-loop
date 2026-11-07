using System;

namespace Samplers;

/// <summary>
/// Rolling average of steering angle in degrees, pushed once per tick.
/// Same shape as SpeedSampler: fixed capacity 120, Average() == 0 when
/// empty. Stub: both methods throw until a candidate implements them.
/// </summary>
public sealed class SteeringSampler
{
    public const int DefaultCapacity = 120;

    private readonly int _capacity;

    public SteeringSampler(int capacity = DefaultCapacity)
    {
        _capacity = capacity;
    }

    public int Capacity => _capacity;

    public void Push(float angleDeg) => throw new NotImplementedException();

    public float Average() => throw new NotImplementedException();
}
