import { supabase } from "../supabaseClient";
import type { GameDataRepository } from "./GameDataRepository";
import type { GameConfig } from "../../domain/game/GameSession";
import { Asset } from "../../domain/market/Asset";
import { AssetType } from "../../domain/market/AssetType";
import type { PricePoint } from "../../domain/market/PricePoint";
import type { CalendarRow } from "../../domain/game/GameCalendar";

export class SupabaseGameDataRepository implements GameDataRepository {

  async fetchConfig(): Promise<GameConfig> {
    const { data, error } = await supabase
      .from("game_config")
      .select("*")
      .eq("config_id", "DEMO_BASIC")
      .single();

    if (error) throw new Error(`fetchConfig 실패: ${error.message}`);

    return {
      configId:           data.config_id,
      initialCash:        data.initial_cash,
      initialDebt:        data.initial_debt,
      monthlySalary:      data.monthly_salary,
      livingCost:         data.living_cost,
      initialStress:      data.initial_stress,
      initialTrust:       data.initial_trust,
      maxInvestmentTurn:  data.max_investment_turn,
      repaymentDueTurn:   data.repayment_due_turn,
    };
  }

  async fetchAssets(): Promise<Asset[]> {
    const { data, error } = await supabase
      .from("assets")
      .select("*")
      .eq("is_tradable", true)
      .order("display_order", { ascending: true });

    if (error) throw new Error(`fetchAssets 실패: ${error.message}`);

    return data.map((row) => new Asset({
      assetId:   row.asset_id,
      name:      row.asset_name,
      assetType: row.asset_type as AssetType,
      currency:  row.currency,
    }));
  }

  async fetchCalendar(): Promise<CalendarRow[]> {
    const { data, error } = await supabase
      .from("game_calendar")
      .select("*")
      .order("turn_index", { ascending: true });

    if (error) throw new Error(`fetchCalendar 실패: ${error.message}`);

    return data.map((row) => ({
      turn_index:             row.turn_index,
      game_date:              row.game_date,
      is_repayment_trigger:   row.is_repayment_trigger,
    }));
  }

  async fetchPrices(): Promise<PricePoint[]> {
    const { data, error } = await supabase
      .from("asset_prices")
      .select("*")
      .order("turn_index", { ascending: true });

    if (error) throw new Error(`fetchPrices 실패: ${error.message}`);

    return data.map((row) => ({
      assetId:    row.asset_id,
      turnIndex:  row.turn_index,
      gameDate:   row.game_date,
      closePrice: row.close_price,
      changeRate: row.change_rate,
      currency:   row.currency,
      openPrice:  row.open_price  ?? undefined,
      highPrice:  row.high_price  ?? undefined,
      lowPrice:   row.low_price   ?? undefined,
      volume:     row.volume      ?? undefined,
    }));
  }
}
