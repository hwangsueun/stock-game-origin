import type { GameDataRepository } from "../infrastructure/storage/GameDataRepository";
import { GameSession } from "../domain/game/GameSession";
import { GameCalendar } from "../domain/game/GameCalendar";
import { Market } from "../domain/market/Market";

export class GameSessionFactory {
  constructor(private readonly repo: GameDataRepository) {}

  async create(): Promise<GameSession> {
    // 4개 테이블 병렬 로드
    const [config, assets, calendarRows, prices] = await Promise.all([
      this.repo.fetchConfig(),
      this.repo.fetchAssets(),
      this.repo.fetchCalendar(),
      this.repo.fetchPrices(),
    ]);

    const calendar = new GameCalendar(calendarRows);
    const market = new Market({ assets, prices, calendar });

    return new GameSession({ config, market });
  }
}
