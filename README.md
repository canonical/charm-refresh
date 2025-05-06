# charm-refresh
> [!CAUTION]
> Developer usage documentation is currently a work in progress and will describe the public interface. Until then, there is no public interface and any usage of charm-refresh is subject to breaking changes in any release—expect for usages explicitly approved by @carlcsaposs-canonical

## Versioning
W.X.Y.Z

W is incremented for changes that break `juju refresh` from the previous W.* versions

X.Y.Z is incremented as a [semantic version](https://semver.org/)—X is incremented for breaking changes to the public interface

If W > 0, the public API is stable—regardless of whether X == 0. (The semantic version rules that normally apply when X == 0 should only be applied when W == 0.) When W is incremented, X must be reset to 0.
