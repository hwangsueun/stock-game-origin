import { GameSession } from "../domain/game/GameSession";

export type AssetValuation = {
  assetId: string;
  name: string;
  quantity: number;
  averageBuyPrice: number;
  currentPrice: number;
  evaluatedValue: number;
  unrealizedPnl: number;
  unrealizedReturnRate: number; // 소수점 (0.032 = 3.2%)
}

export type PortfolioValuation = {
  cash: number;
  investmentValue: number;   // 보유 종목 평가금액 합계
  totalAssets: number;       // cash + investmentValue
  totalDebt: number;         // 남은 대출금
  netAssets: number;         // totalAssets - totalDebt
  items: AssetValuation[];
}

export class PortfolioValuationService {
  evaluate(session: GameSession): PortfolioValuation {
    const turn = session.investmentTurn;
    const heldPositions = session.portfolio.getHeldPositions();

    const items: AssetValuation[] = heldPositions.map((pos) => {
      const pricePoint = session.market.getPrice(pos.assetId, turn);
      const currentPrice = pricePoint.closePrice;
      const asset = session.market.getAsset(pos.assetId);

      return {
        assetId: pos.assetId,
        name: asset.name,
        quantity: pos.quantity,
        averageBuyPrice: pos.averageBuyPrice,
        currentPrice,
        evaluatedValue: pos.evaluatedValue(currentPrice),
        unrealizedPnl: pos.unrealizedPnl(currentPrice),
        unrealizedReturnRate: pos.unrealizedReturnRate(currentPrice),
      };
    });

    const investmentValue = items.reduce((sum, item) => sum + item.evaluatedValue, 0);
    const cash = session.player.cash;
    const totalAssets = cash + investmentValue;
    const totalDebt = session.loan.remainingDebt;

    return {
      cash,
      investmentValue,
      totalAssets,
      totalDebt,
      netAssets: totalAssets - totalDebt,
      items,
    };
  }
}
