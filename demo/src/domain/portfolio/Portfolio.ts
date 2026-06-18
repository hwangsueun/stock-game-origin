import { Position } from "./Position";

export class Portfolio {
  private readonly _positions: Map<string, Position>;

  constructor() {
    this._positions = new Map();
  }

  /** 특정 종목 포지션 (없으면 빈 포지션 반환) */
  getPosition(assetId: string): Position {
    if (!this._positions.has(assetId)) {
      this._positions.set(
        assetId,
        new Position({ assetId, quantity: 0, averageBuyPrice: 0 })
      );
    }
    return this._positions.get(assetId)!;
  }

  /** 보유 종목만 (수량 > 0) */
  getHeldPositions(): Position[] {
    return Array.from(this._positions.values()).filter((p) => !p.isEmpty());
  }

  /** 전체 포지션 (빈 것 포함) */
  getAllPositions(): Position[] {
    return Array.from(this._positions.values());
  }

  hasPosition(assetId: string): boolean {
    const pos = this._positions.get(assetId);
    return pos !== undefined && !pos.isEmpty();
  }

  /** 매수 처리 */
  applyBuy(assetId: string, quantity: number, price: number): void {
    this.getPosition(assetId).buy(quantity, price);
  }

  /** 전량 매도 처리 — 실현손익 반환 */
  applySellAll(assetId: string, currentPrice: number): { proceeds: number; realizedPnl: number } {
    const pos = this._positions.get(assetId);
    if (!pos || pos.isEmpty()) {
      throw new Error(`Portfolio.applySellAll: no position for ${assetId}`);
    }
    return pos.sellAll(currentPrice);
  }

  /** 전체 평가금액 (현재 가격 map 필요) */
  totalEvaluatedValue(prices: Map<string, number>): number {
    let total = 0;
    for (const pos of this.getHeldPositions()) {
      const price = prices.get(pos.assetId);
      if (price === undefined)
        throw new Error(`Portfolio: price not found for ${pos.assetId}`);
      total += pos.evaluatedValue(price);
    }
    return total;
  }
}
