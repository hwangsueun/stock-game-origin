export class Position {
  readonly assetId: string;
  private _quantity: number;
  private _averageBuyPrice: number;

  constructor(params: {
    assetId: string;
    quantity: number;
    averageBuyPrice: number;
  }) {
    if (params.quantity < 0) throw new Error("Position: quantity must be >= 0");
    if (params.averageBuyPrice < 0)
      throw new Error("Position: averageBuyPrice must be >= 0");
    this.assetId = params.assetId;
    this._quantity = params.quantity;
    this._averageBuyPrice = params.averageBuyPrice;
  }

  get quantity(): number {
    return this._quantity;
  }

  get averageBuyPrice(): number {
    return this._averageBuyPrice;
  }

  isEmpty(): boolean {
    return this._quantity === 0;
  }

  /** 매수 — 평단가 갱신 */
  buy(quantity: number, price: number): void {
    if (quantity <= 0) throw new Error("Position.buy: quantity must be > 0");
    if (price <= 0) throw new Error("Position.buy: price must be > 0");

    const prevCost = this._averageBuyPrice * this._quantity;
    const newCost = price * quantity;
    this._quantity += quantity;
    this._averageBuyPrice = (prevCost + newCost) / this._quantity;
  }

  /** 전량 매도 — 실현손익 반환 */
  sellAll(currentPrice: number): { proceeds: number; realizedPnl: number } {
    if (this._quantity === 0) throw new Error("Position.sellAll: no position to sell");
    const proceeds = currentPrice * this._quantity;
    const realizedPnl = (currentPrice - this._averageBuyPrice) * this._quantity;
    this._quantity = 0;
    this._averageBuyPrice = 0;
    return { proceeds, realizedPnl };
  }

  /** 평가금액 */
  evaluatedValue(currentPrice: number): number {
    return currentPrice * this._quantity;
  }

  /** 미실현손익 */
  unrealizedPnl(currentPrice: number): number {
    return (currentPrice - this._averageBuyPrice) * this._quantity;
  }

  /** 미실현 수익률 (소수점) */
  unrealizedReturnRate(currentPrice: number): number {
    if (this._averageBuyPrice === 0) return 0;
    return (currentPrice - this._averageBuyPrice) / this._averageBuyPrice;
  }
}
