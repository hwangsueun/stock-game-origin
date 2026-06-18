import { useEffect } from "react";
import { GamePhase } from "../../domain/game/GamePhase";
import { useGameStore } from "../store/useGameStore";
import { StartStoryView } from "../components/story/StartStoryView";
import { InvestmentDashboard } from "../components/investment/InvestmentDashboard";
import { RepaymentView } from "../components/repayment/RepaymentView";
import { DemoResultView } from "../components/result/DemoResultView";
import { PixelPanel } from "../components/layout/PixelPanel";

export function GamePage() {
  const {
    snapshot,
    loading,
    error,
    initGame,
    startInvestment,
    buy,
    sellAll,
    hold,
    endTurn,
    repay,
  } = useGameStore();

  useEffect(() => {
    void initGame();
  }, [initGame]);

  if (loading) {
    return (
      <main style={pageStyle}>
        <PixelPanel>
          <h2>데모 로딩 중...</h2>
          <p>Supabase에서 게임 데이터를 불러오고 있습니다.</p>
        </PixelPanel>
      </main>
    );
  }

  if (error) {
    return (
      <main style={pageStyle}>
        <PixelPanel>
          <h2>오류 발생</h2>
          <p style={{ color: "#ff6b6b" }}>{error}</p>
          <button onClick={() => void initGame()} style={buttonStyle}>
            다시 불러오기
          </button>
        </PixelPanel>
      </main>
    );
  }

  if (!snapshot) {
    return (
      <main style={pageStyle}>
        <PixelPanel>
          <h2>게임 세션 없음</h2>
          <button onClick={() => void initGame()} style={buttonStyle}>
            게임 시작 준비
          </button>
        </PixelPanel>
      </main>
    );
  }

  return (
    <main style={pageStyle}>
      {snapshot.phase === GamePhase.START_STORY && (
        <StartStoryView snapshot={snapshot} onComplete={startInvestment} />
      )}

      {snapshot.phase === GamePhase.INVESTMENT && (
        <InvestmentDashboard
          snapshot={snapshot}
          onBuy={buy}
          onSellAll={sellAll}
          onHold={hold}
          onEndTurn={endTurn}
        />
      )}

      {snapshot.phase === GamePhase.REPAYMENT && (
        <RepaymentView snapshot={snapshot} onRepay={repay} />
      )}

      {snapshot.phase === GamePhase.DEMO_RESULT && (
        <DemoResultView snapshot={snapshot} />
      )}
    </main>
  );
}

const pageStyle: React.CSSProperties = {
  minHeight: "100vh",
  background: "#171717",
  color: "#f5f5f5",
  padding: "24px",
  fontFamily:
    "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
};

const buttonStyle: React.CSSProperties = {
  padding: "10px 14px",
  border: "2px solid #f7e72f",
  background: "#fbaf45",
  color: "#171717",
  fontWeight: 800,
  cursor: "pointer",
};