using StardewModdingAPI;
using StardewModdingAPI.Events;

namespace StardewMemoryExporter
{
    public class ModEntry : Mod
    {
        private StardewObserver _observer;
        private StardewExecutor _executor;

        private SharedBlackboard _blackboard;


        public override void Entry(IModHelper helper)
        {
            _blackboard = new SharedBlackboard();
            _observer = new StardewObserver(this.Monitor, _blackboard, 9999);
            _executor = new StardewExecutor(this.Monitor, _blackboard, this.Helper, 8888);


            helper.Events.GameLoop.UpdateTicked += OnUpdateTicked;
            helper.Events.GameLoop.SaveLoaded += (s, a) => this.Monitor.Log("🎮 存档载入，agent 接口链路完全激活！", LogLevel.Info);
        }

        private void OnUpdateTicked(object sender, UpdateTickedEventArgs e)
        {
            _observer.PulseGameMemory();
            // _observer.ListenHudMessages(e);
            _executor.ListenDialogMessages(e);
        }

        protected override void Dispose(bool disposing)
        {
            _observer?.CleanUp();
            _executor?.CleanUp();
            base.Dispose(disposing);
        }
    }
}