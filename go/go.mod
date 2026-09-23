module github.com/kangminlee-maker/haltrule/go

// Nothing goes above that line. The import path is where the module is fetched
// from, which for Go is the repository and the directory inside it; `go get`
// needs nothing else, and the version is a tag named go/v0.0.1. gremlins reads
// the first line of this file and takes whatever follows `module ` as the
// module path, so a comment there leaves it with a sentence instead: it then
// strips that sentence from the coverage profile's file names, strips nothing,
// finds no file it knows, and calls every mutant one no run reaches. Measured:
// 313 of them, all at once.

go 1.22
