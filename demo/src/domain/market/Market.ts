import { Asset } from "./Asset";
import type { PricePoint } from "./PricePoint";
import { GameCalendar } from "../game/GameCalendar";

export class Market {
  private readonly _assets: Map<string, Asset>;
  private readonly _prices: Map<string, PricePoint>; // key: `${assetId}:${turnIndex}`
  readonly calendar: GameCalendar;

  constructor(params: {
    assets: Asset[];
    prices: PricePoint[];
    calendar: GameCalendar;
  }) {
    this._assets = new Map(params.assets.map((a) => [a.assetId, a]));
    this._prices = new Map(
      params.prices.map((p) => [`${p.assetId}:${p.turnIndex}`, p])
    );
    this.calendar = params.calendar;
  }

  getAsset(assetId: string): Asset {
    const asset = this._assets.get(assetId);
    if (!asset) throw new Error(`Market: asset not found — ${assetId}`);
    return asset;
  }

  getAllAssets(): Asset[] {
    return Array.from(this._assets.values());
  }

  getPrice(assetId: string, turnIndex: number): PricePoint {
    const key = `${assetId}:${turnIndex}`;
    const price = this._prices.get(key);
    if (!price)
      throw new Error(`Market: price not found — assetId=${assetId}, turn=${turnIndex}`);
    return price;
  }

  getAllPricesForTurn(turnIndex: number): PricePoint[] {
    return Array.from(this._prices.values()).filter(
      (p) => p.turnIndex === turnIndex
    );
  }

  /** 특정 종목의 전체 가격 이력 (차트용) */
  getPriceHistory(assetId: string): PricePoint[] {
    return Array.from(this._prices.values())
      .filter((p) => p.assetId === assetId)
      .sort((a, b) => a.turnIndex - b.turnIndex);
  }

  hasAsset(assetId: string): boolean {
    return this._assets.has(assetId);
  }
}
