import { supabase } from "../supabaseClient";
import { NewsItem } from "../../domain/news/NewsItem";
import { NewsSentiment } from "../../domain/news/NewsSentiment";
import type { NewsQuery, NewsRepository } from "./NewsRepository";

type NewsRow = {
  id: string;
  turn_index: number;
  game_date: string;
  asset_id: string | null;
  headline: string;
  body: string;
  asset_class: string | null;
  sentiment: string;
  importance: number;
  news_order: number | null;
};

export class SupabaseNewsRepository implements NewsRepository {
  async findByTurn(query: NewsQuery): Promise<NewsItem[]> {
    if (query.turnIndex <= 0) {
      return [];
    }

    const limit = query.limit ?? 5;
    const includeGlobal = query.includeGlobal ?? true;

    let request = supabase
      .from("news")
      .select(
        [
          "id",
          "turn_index",
          "game_date",
          "asset_id",
          "headline",
          "body",
          "asset_class",
          "sentiment",
          "importance",
          "news_order",
        ].join(","),
      )
      .eq("turn_index", query.turnIndex);

    if (query.assetId && includeGlobal) {
      request = request.or(`asset_id.is.null,asset_id.eq.${query.assetId}`);
    } else if (query.assetId && !includeGlobal) {
      request = request.eq("asset_id", query.assetId);
    }

    const { data, error } = await request
      .order("importance", { ascending: false })
      .order("news_order", { ascending: true })
      .limit(limit);

    if (error) {
      throw new Error(`뉴스 조회 실패: ${error.message}`);
    }

    return ((data ?? []) as NewsRow[]).map(
      (row) =>
        new NewsItem({
          id: row.id,
          turnIndex: row.turn_index,
          gameDate: row.game_date,
          assetId: row.asset_id,
          headline: row.headline,
          body: row.body,
          assetClass: row.asset_class,
          sentiment: this.toSentiment(row.sentiment),
          importance: row.importance,
          newsOrder: row.news_order,
        }),
    );
  }

  private toSentiment(value: string): NewsSentiment {
    if (value === NewsSentiment.POSITIVE) {
      return NewsSentiment.POSITIVE;
    }

    if (value === NewsSentiment.NEGATIVE) {
      return NewsSentiment.NEGATIVE;
    }

    return NewsSentiment.NEUTRAL;
  }
}