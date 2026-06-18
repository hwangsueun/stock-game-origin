import type { GameConfig } from "../../domain/game/GameSession";
import { Asset } from "../../domain/market/Asset";
import type { PricePoint } from "../../domain/market/PricePoint";
import type { CalendarRow } from "../../domain/game/GameCalendar";

export type GameDataRepository = {
  fetchConfig(): Promise<GameConfig>;
  fetchAssets(): Promise<Asset[]>;
  fetchCalendar(): Promise<CalendarRow[]>;
  fetchPrices(): Promise<PricePoint[]>;
}
