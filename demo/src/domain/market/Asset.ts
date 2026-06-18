import { AssetType } from "./AssetType";

export class Asset {
  readonly assetId: string;
  readonly name: string;
  readonly assetType: AssetType;
  readonly currency: string;
  readonly description: string;

  constructor(params: {
    assetId: string;
    name: string;
    assetType: AssetType;
    currency: string;
    description?: string;
  }) {
    if (!params.assetId) throw new Error("Asset: assetId is required");
    if (!params.name) throw new Error("Asset: name is required");
    this.assetId = params.assetId;
    this.name = params.name;
    this.assetType = params.assetType;
    this.currency = params.currency;
    this.description = params.description ?? "";
  }
}