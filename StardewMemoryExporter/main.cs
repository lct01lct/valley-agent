using System;
using StardewModdingAPI;
using StardewModdingAPI.Events;

namespace StardewMemoryExporter
{
    public class ModEntry : Mod
    {
        private StardewObserver _observer;
        private StardewExecutor _executor;

        public override void Entry(IModHelper helper)
        {
            _observer = new StardewObserver(this.Monitor, 9999);
            _executor = new StardewExecutor(this.Monitor, this.Helper, 8888);

            helper.Events.GameLoop.UpdateTicked += OnUpdateTicked;
            helper.Events.GameLoop.SaveLoaded += (s, a) => this.Monitor.Log("🎮 存档载入，智能体脑机接口链路完全激活！", LogLevel.Info);
        }

        private void OnUpdateTicked(object sender, UpdateTickedEventArgs e)
        {
            _observer.PulseGameMemory();
            _executor.UpdateMovementTick();
        }

        protected override void Dispose(bool disposing)
        {
            // 优雅下线，随游戏关闭释放所有的物理 Socket 端口绑定
            _observer?.CleanUp();
            _executor?.CleanUp();
            base.Dispose(disposing);
        }
    }
}