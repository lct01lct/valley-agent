
using System;
using System.Collections.Generic;

public class SharedBlackboard
{
    public int FrameTimeoutCounter = 0;
    // 门交互相关
    public bool IsWaitingForDoorResponse = false;

    // HUD 消息相关
    public List<string> ProcessedMessages { get; set; } = new List<string>();
    public List<string> LastHudMessages { get; set; } = new List<string>();

    // 线程安全
    private readonly object _lock = new object();

    public T Get<T>(string key)
    {
        lock (_lock)
        {
            // 使用字典存储任意类型的数据
            if (_data.TryGetValue(key, out object value))
                return (T)value;
            return default;
        }
    }

    public void Set(string key, object value)
    {
        lock (_lock)
        {
            _data[key] = value;
        }
    }


    private Dictionary<string, object> _data = new Dictionary<string, object>();
}
