import { GameSession } from "../../domain/game/GameSession";
import { GamePhase } from "../../domain/game/GamePhase";
import { PortfolioValuationService } from "../../services/PortfolioValuationService";
import type { GameSnapshot, MarketRow } from "./GameSnapshot";

const valuationService = new PortfolioValuationService();

export function buildSnapshot(session: GameSession): GameSnapshot {
  const turn = session.investmentTurn;

  // 시장 데이터 — INVESTMENT/REPAYMENT에서는 현재 턴, START_STORY는 1턴 기준
  const turnForMarket = turn > 0 ? turn : 1;

  const marketRows: MarketRow[] = session.market.getAllAssets().map((asset) => {
    const price = session.market.getPrice(asset.assetId, turnForMarket);
    return {
      assetId:    asset.assetId,
      name:       asset.name,
      assetType:  asset.assetType,
      closePrice: price.closePrice,
      changeRate: price.changeRate,
      currency:   price.currency,
    };
  });

  // 포트폴리오 평가 — 투자 중일 때만 의미 있음
  let investmentValue = 0;
  let totalAssets = 0;
  let netAssets = 0;
  let holdings: GameSnapshot["holdings"] = [];

  if (session.phase !== GamePhase.START_STORY && turn > 0) {
    const valuation = valuationService.evaluate(session);
    investmentValue = valuation.investmentValue;
    totalAssets     = valuation.totalAssets;
    netAssets       = valuation.netAssets;
    holdings        = valuation.items;
  } else {
    totalAssets = session.player.cash;
    netAssets   = session.player.cash - session.loan.remainingDebt;
  }

  const currentDate =
    turn > 0
      ? session.market.calendar.getDate(turnForMarket)
      : session.market.calendar.getDate(1);

  return {
    phase:          session.phase,
    investmentTurn: turn,
    currentDate,
    maxTurn:        session.config.maxInvestmentTurn,

    cash:           session.player.cash,
    stress:         session.player.stress.value,
    trust:          session.player.trust.value,

    initialDebt:    session.loan.initialDebt,
    remainingDebt:  session.loan.remainingDebt,

    marketRows,
    investmentValue,
    totalAssets,
    netAssets,
    holdings,

    logs: session.logs,
  };
}
