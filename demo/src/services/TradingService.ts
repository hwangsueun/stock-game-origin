import { GameSession } from "../domain/game/GameSession";

export class TradingService {
  /**
   * 금액 기준 매수
   * - cashAmount 만큼 현금으로 해당 종목 매수
   * - 살 수 있는 수량(floor)만큼만 매수
   */
  buyByAmount(session: GameSession, assetId: string, cashAmount: number): void {
    if (cashAmount <= 0) throw new Error("TradingService.buyByAmount: cashAmount must be > 0");

    const turn = session.investmentTurn;
    const pricePoint = session.market.getPrice(assetId, turn);
    const price = pricePoint.closePrice;

    const quantity = Math.floor(cashAmount / price);
    if (quantity <= 0) {
      throw new Error(
        `TradingService.buyByAmount: cashAmount(${cashAmount})이 현재 가격(${price})보다 낮아 매수 불가`
      );
    }

    const actualCost = price * quantity;
    session.player.deductCash(actualCost);
    session.portfolio.applyBuy(assetId, quantity, price);

    const asset = session.market.getAsset(assetId);
    session.addLog({
      turnIndex: turn,
      logType: "BUY",
      assetId,
      amount: actualCost,
      message: `[매수] ${asset.name} ${quantity}주 @ ${price.toLocaleString()}원 = ${actualCost.toLocaleString()}원`,
    });
  }

  /**
   * 전량 매도
   */
  sellAll(session: GameSession, assetId: string): void {
    if (!session.portfolio.hasPosition(assetId)) {
      throw new Error(`TradingService.sellAll: ${assetId} 포지션 없음`);
    }

    const turn = session.investmentTurn;
    const pricePoint = session.market.getPrice(assetId, turn);
    const price = pricePoint.closePrice;

    const { proceeds, realizedPnl } = session.portfolio.applySellAll(assetId, price);
    session.player.addCash(proceeds);

    const asset = session.market.getAsset(assetId);
    const pnlStr = realizedPnl >= 0
      ? `+${realizedPnl.toLocaleString()}`
      : realizedPnl.toLocaleString();

    session.addLog({
      turnIndex: turn,
      logType: "SELL",
      assetId,
      amount: proceeds,
      message: `[매도] ${asset.name} 전량 매도 → 수령액 ${proceeds.toLocaleString()}원 (실현손익 ${pnlStr}원)`,
    });
  }
}